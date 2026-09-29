"""Read-only Gmail inbox access using OAuth 2.0."""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CREDENTIALS_DIR = Path(__file__).resolve().parent / "credentials"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
OAUTH_CLIENT_FILE = CREDENTIALS_DIR / "gmail-oauth-web-client.json"
TOKEN_FILE = CREDENTIALS_DIR / "gmail-token.json"
OAUTH_CALLBACK_PORT = 8000
OAUTH_CALLBACK_PATH = "/oauth2callback"
OAUTH_CALLBACK_TIMEOUT_SECONDS = 600


class GmailServiceError(RuntimeError):
	"""Base error for Gmail OAuth, API, and message parsing failures."""


class GmailCredentialsMissingError(GmailServiceError):
	"""Raised when the OAuth client credentials file is missing."""


class GmailOAuthError(GmailServiceError):
	"""Raised when Gmail OAuth authentication fails."""


class GmailAPIError(GmailServiceError):
	"""Raised when a Gmail API request fails."""


class MalformedEmailError(GmailServiceError):
	"""Raised when a Gmail message does not contain usable email content."""


class _HTMLTextExtractor(HTMLParser):
	def __init__(self) -> None:
		super().__init__()
		self.text_parts: list[str] = []

	def handle_data(self, data: str) -> None:
		self.text_parts.append(data)


@dataclass(frozen=True)
class GmailMessage:
	message_id: str
	sender: str
	sender_name: str
	sender_email: str
	subject: str
	body: str

	@property
	def customer_name(self) -> str:
		return self.sender_name or self.sender_email


def _decode_header(value: str) -> str:
	try:
		return str(make_header(decode_header(value)))
	except (LookupError, UnicodeError, ValueError) as exc:
		raise MalformedEmailError("Email contains an invalid encoded header") from exc


def _decode_body_part(data: Any) -> str:
	if not isinstance(data, str):
		raise MalformedEmailError("Email body contains invalid base64 data")
	try:
		padded_data = data + "=" * (-len(data) % 4)
		return base64.urlsafe_b64decode(padded_data).decode("utf-8", errors="replace")
	except (binascii.Error, ValueError) as exc:
		raise MalformedEmailError("Email body contains invalid base64 data") from exc


def _collect_body_parts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
	plain_parts: list[str] = []
	html_parts: list[str] = []
	body = payload.get("body") or {}
	data = body.get("data") if isinstance(body, dict) else None
	mime_type = str(payload.get("mimeType", "")).casefold()

	if data:
		decoded = _decode_body_part(data)
		if mime_type == "text/plain":
			plain_parts.append(decoded)
		elif mime_type == "text/html":
			extractor = _HTMLTextExtractor()
			extractor.feed(decoded)
			html_parts.append(" ".join(extractor.text_parts))

	parts = payload.get("parts") or []
	if not isinstance(parts, list):
		raise MalformedEmailError("Email contains malformed body parts")
	for part in parts:
		if not isinstance(part, dict):
			raise MalformedEmailError("Email contains malformed body parts")
		child_plain, child_html = _collect_body_parts(part)
		plain_parts.extend(child_plain)
		html_parts.extend(child_html)
	return plain_parts, html_parts


def parse_gmail_message(message: dict[str, Any]) -> GmailMessage:
	"""Extract sender, subject, and a readable body from a Gmail API message."""
	if not isinstance(message, dict):
		raise MalformedEmailError("Gmail returned a malformed message")
	payload = message.get("payload")
	if not isinstance(payload, dict):
		raise MalformedEmailError("Gmail message has no valid payload")

	headers = payload.get("headers") or []
	if not isinstance(headers, list):
		raise MalformedEmailError("Gmail message contains malformed headers")
	header_map = {
		str(header.get("name", "")).casefold(): str(header.get("value", ""))
		for header in headers
		if isinstance(header, dict)
	}
	from_header = _decode_header(header_map.get("from", "").strip())
	if not from_header:
		raise MalformedEmailError("Gmail message is missing its sender")
	sender_name, sender_email = parseaddr(from_header)
	if not sender_email:
		raise MalformedEmailError("Gmail message has an invalid sender address")
	if not sender_name:
		sender_name = ""
	subject = _decode_header(header_map.get("subject", "(No subject)"))
	plain_parts, html_parts = _collect_body_parts(payload)
	body_text = "\n".join(plain_parts).strip()
	if not body_text:
		body_text = "\n".join(html_parts).strip()
	if not body_text:
		raise MalformedEmailError("Gmail message does not contain a readable body")

	message_id = message.get("id")
	if not isinstance(message_id, str) or not message_id:
		raise MalformedEmailError("Gmail message is missing its ID")
	return GmailMessage(
		message_id=message_id,
		sender=from_header,
		sender_name=sender_name.strip(),
		sender_email=sender_email.strip(),
		subject=subject.strip() or "(No subject)",
		body=body_text,
	)


def truncate_body_for_display(body: str, max_chars: int = 1000) -> str:
	"""Bound body output and remove terminal control characters."""
	if max_chars < 1:
		raise ValueError("max_chars must be positive")
	safe_body = "".join(
		character
		for character in body
		if character in "\n\t" or ord(character) >= 32
	)
	if len(safe_body) <= max_chars:
		return safe_body
	return safe_body[:max_chars].rstrip() + "... [truncated]"


class GmailReadOnlyService:
	"""Authenticate with Gmail and expose inbox listing and message reading only."""

	def __init__(
		self,
		credentials_file: Path = OAUTH_CLIENT_FILE,
		token_file: Path = TOKEN_FILE,
		gmail_client: Any | None = None,
	) -> None:
		self.credentials_file = Path(credentials_file)
		self.token_file = Path(token_file)
		if gmail_client is not None:
			self._service = gmail_client
			return

		credentials = self._authorize()
		try:
			self._service = build(
				"gmail", "v1", credentials=credentials, cache_discovery=False
			)
		except Exception as exc:
			raise GmailAPIError("Could not initialize the Gmail API client") from exc

	def _authorize(self) -> Credentials:
		redirect_uri = self.gmail_redirect_uri()
		client_config = self._load_web_client_config()
		credentials = None
		if self.token_file.exists():
			try:
				credentials = Credentials.from_authorized_user_file(
					str(self.token_file), [GMAIL_READONLY_SCOPE]
				)
			except (OSError, ValueError, GoogleAuthError):
				credentials = None
			if credentials and credentials.client_id != client_config["client_id"]:
				credentials = None

		if credentials and credentials.expired and credentials.refresh_token:
			try:
				credentials.refresh(Request())
			except GoogleAuthError:
				credentials = None

		if credentials and credentials.valid:
			return credentials

		try:
			flow = Flow.from_client_secrets_file(
				str(self.credentials_file), scopes=[GMAIL_READONLY_SCOPE]
			)
			flow.redirect_uri = redirect_uri
			authorization_url, _ = flow.authorization_url(
				access_type="offline", prompt="consent"
			)
		except Exception as exc:
			raise GmailOAuthError("Could not prepare the Gmail OAuth authorization") from exc

		result: dict[str, Any] = {"done": Event(), "credentials": None, "error": None}
		self._run_codespaces_callback(flow, redirect_uri, result, authorization_url)
		if result["error"]:
			raise GmailOAuthError(str(result["error"]))
		credentials = result["credentials"]
		if not isinstance(credentials, Credentials):
			raise GmailOAuthError("Gmail OAuth callback returned no credentials")

		try:
			self.token_file.parent.mkdir(parents=True, exist_ok=True)
			self.token_file.write_text(credentials.to_json(), encoding="utf-8")
		except OSError as exc:
			raise GmailOAuthError("Could not save the local Gmail OAuth token") from exc
		return credentials

	def _load_web_client_config(self) -> dict[str, Any]:
		if not self.credentials_file.is_file():
			raise GmailCredentialsMissingError(
				"Gmail OAuth Web application client file is missing. Place the downloaded "
			f"Web client JSON at: {self.credentials_file}"
			)
		try:
			client_secrets = json.loads(self.credentials_file.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as exc:
			raise GmailOAuthError("Could not read the Gmail OAuth client JSON") from exc
		web_config = client_secrets.get("web") if isinstance(client_secrets, dict) else None
		if not isinstance(web_config, dict) or not web_config.get("client_id"):
			raise GmailOAuthError(
				"Codespaces requires a Google Web application OAuth client. Place its "
			f"JSON at {self.credentials_file}; the Desktop client file can remain "
			"untouched for reference."
			)
		return web_config

	@staticmethod
	def gmail_redirect_uri() -> str:
		redirect_uri = dotenv_values(ENV_FILE).get("GMAIL_REDIRECT_URI")
		if not redirect_uri:
			raise GmailOAuthError(f"GMAIL_REDIRECT_URI is missing from {ENV_FILE}")

		parsed_uri = urlsplit(redirect_uri)
		if (
			parsed_uri.scheme != "https"
			or not parsed_uri.netloc
			or parsed_uri.path != OAUTH_CALLBACK_PATH
			or parsed_uri.query
			or parsed_uri.fragment
		):
			raise GmailOAuthError(
				"GMAIL_REDIRECT_URI must be an HTTPS URL ending in "
				f"{OAUTH_CALLBACK_PATH}"
			)
		return redirect_uri

	def _run_codespaces_callback(
		self,
		flow: Flow,
		redirect_uri: str,
		result: dict[str, Any],
		authorization_url: str,
	) -> None:
		completion_event: Event = result["done"]

		class OAuthCallbackHandler(BaseHTTPRequestHandler):
			def do_GET(self) -> None:
				request_uri = urlsplit(self.path)
				if request_uri.path != OAUTH_CALLBACK_PATH:
					self.send_error(404)
					return

				query_params = parse_qs(request_uri.query, keep_blank_values=True)
				code_values = query_params.get("code", [])
				state_values = query_params.get("state", [])
				if not code_values or len(code_values) != 1:
					status = 400
					body = "Gmail authorization failed: missing OAuth code."
					result["error"] = "Missing OAuth code in the callback request."
					self._finish_callback(status, body)
					completion_event.set()
					return

				if not state_values or len(state_values) != 1:
					status = 400
					body = "Gmail authorization failed: missing OAuth state."
					result["error"] = "Missing OAuth state in the callback request."
					self._finish_callback(status, body)
					completion_event.set()
					return

				code = code_values[0]
				state = state_values[0]
				expected_state = getattr(flow, "state", None) or getattr(flow, "_state", None)
				if expected_state is None or state != expected_state:
					status = 400
					body = "Gmail authorization failed: invalid OAuth state."
					result["error"] = "OAuth state mismatch during callback verification."
					self._finish_callback(status, body)
					completion_event.set()
					return

				try:
					flow.fetch_token(code=code, state=state)
					credentials = flow.credentials
					result["credentials"] = credentials
					status = 200
					body = (
						"Gmail authorization succeeded. You can close this browser tab "
						"and return to the Codespace terminal."
					)
				except Exception:
					status = 400
					result["error"] = (
						"Gmail OAuth callback verification failed. Check the authorized "
						"redirect URI and retry authorization."
					)
					body = "Gmail authorization failed. Return to the Codespace terminal."
				self._finish_callback(status, body)
				completion_event.set()

			def _finish_callback(self, status: int, body: str) -> None:
				self.send_response(status)
				self.send_header("Content-Type", "text/plain; charset=utf-8")
				self.end_headers()
				self.wfile.write(body.encode("utf-8"))

			def log_message(self, _format: str, *_args: Any) -> None:
				return

		try:
			server = HTTPServer(("0.0.0.0", OAUTH_CALLBACK_PORT), OAuthCallbackHandler)
		except OSError as exc:
			raise GmailOAuthError(
				f"Could not bind the OAuth callback port {OAUTH_CALLBACK_PORT}; "
				"check whether another process is using it."
			) from exc

		print("Open this URL in your browser to authorize read-only Gmail access:")
		print(authorization_url)
		print(f"Waiting for OAuth callback at {redirect_uri}")
		server.timeout = 1
		deadline = time.monotonic() + OAUTH_CALLBACK_TIMEOUT_SECONDS
		try:
			while not completion_event.is_set() and time.monotonic() < deadline:
				server.handle_request()
		finally:
			server.server_close()
		if not completion_event.is_set():
			raise GmailOAuthError(
				"Timed out waiting for the Codespaces OAuth callback. Check that port "
				f"{OAUTH_CALLBACK_PORT} is forwarded and retry."
			)

	def list_recent_inbox_messages(self, max_results: int = 5) -> list[GmailMessage]:
		"""Fetch at most max_results recent inbox messages, without modifying them."""
		if not 1 <= max_results <= 10:
			raise ValueError("max_results must be between 1 and 10")
		try:
			response = (
				self._service.users()
				.messages()
				.list(userId="me", labelIds=["INBOX"], maxResults=max_results)
				.execute()
			)
		except HttpError as exc:
			raise GmailAPIError("Gmail could not list inbox messages") from exc
		except Exception as exc:
			raise GmailAPIError("Gmail inbox request failed") from exc

		message_refs = response.get("messages", []) if isinstance(response, dict) else []
		if not isinstance(message_refs, list):
			raise GmailAPIError("Gmail returned a malformed inbox result")

		messages: list[GmailMessage] = []
		for message_ref in message_refs[:max_results]:
			if not isinstance(message_ref, dict) or not message_ref.get("id"):
				raise MalformedEmailError("Gmail returned a malformed message reference")
			try:
				message = (
					self._service.users()
					.messages()
					.get(userId="me", id=message_ref["id"], format="full")
					.execute()
				)
			except HttpError as exc:
				raise GmailAPIError("Gmail could not read an inbox message") from exc
			except Exception as exc:
				raise GmailAPIError("Gmail message request failed") from exc
			messages.append(parse_gmail_message(message))
		return messages