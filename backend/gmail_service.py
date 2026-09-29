"""Read-only Gmail inbox access using OAuth 2.0."""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CREDENTIALS_DIR = Path(__file__).resolve().parent / "credentials"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
OAUTH_CLIENT_FILE = CREDENTIALS_DIR / "gmail-oauth-web-client.json"
TOKEN_FILE = CREDENTIALS_DIR / "gmail-token.json"
OAUTH_CALLBACK_PATH = "/oauth2callback"


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
		credentials: Credentials | None = None,
	) -> None:
		self.credentials_file = Path(credentials_file)
		self.token_file = Path(token_file)
		if gmail_client is not None:
			self._service = gmail_client
			return

		credentials = credentials or self._load_local_credentials()
		if credentials and credentials.expired and credentials.refresh_token:
			try:
				credentials.refresh(Request())
			except GoogleAuthError:
				credentials = None
		if credentials is None or not credentials.valid:
			raise GmailOAuthError(
				"Gmail is not authorized. Visit /oauth2/start to connect a Gmail account."
			)
		try:
			self._service = build(
				"gmail", "v1", credentials=credentials, cache_discovery=False
			)
		except Exception as exc:
			raise GmailAPIError("Could not initialize the Gmail API client") from exc

	def _load_local_credentials(self) -> Credentials | None:
		credentials = None
		if self.token_file.exists():
			try:
				credentials = Credentials.from_authorized_user_file(
					str(self.token_file), [GMAIL_READONLY_SCOPE]
				)
			except (OSError, ValueError, GoogleAuthError):
				credentials = None

		if credentials and credentials.expired and credentials.refresh_token:
			try:
				credentials.refresh(Request())
			except GoogleAuthError:
				credentials = None

		if credentials and credentials.valid:
			return credentials
		return None

	@staticmethod
	def load_web_client_config(
		credentials_file: Path = OAUTH_CLIENT_FILE,
	) -> dict[str, Any]:
		client_json = os.getenv("GMAIL_OAUTH_CLIENT_JSON")
		if client_json is None:
			client_json = dotenv_values(ENV_FILE).get("GMAIL_OAUTH_CLIENT_JSON")
		try:
			if client_json is not None:
				client_secrets = json.loads(client_json)
			elif credentials_file.is_file():
				client_secrets = json.loads(
					credentials_file.read_text(encoding="utf-8")
				)
			else:
				raise GmailCredentialsMissingError(
					"Gmail OAuth Web client JSON is missing. Set GMAIL_OAUTH_CLIENT_JSON "
					"or provide the local Web client JSON file."
				)
		except (OSError, json.JSONDecodeError) as exc:
			raise GmailOAuthError("Could not read the Gmail OAuth client JSON") from exc
		web_config = client_secrets.get("web") if isinstance(client_secrets, dict) else None
		if not isinstance(web_config, dict) or not web_config.get("client_id"):
			raise GmailOAuthError(
				"Gmail OAuth requires a Google Web application client configuration."
			)
		return {"web": web_config}

	@staticmethod
	def gmail_redirect_uri() -> str:
		redirect_uri = os.getenv("GMAIL_REDIRECT_URI") or dotenv_values(ENV_FILE).get(
			"GMAIL_REDIRECT_URI"
		)
		if not redirect_uri:
			raise GmailOAuthError("GMAIL_REDIRECT_URI is not configured")

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