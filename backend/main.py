"""FastAPI application for read-only Gmail access."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from threading import Lock

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse, RedirectResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

if __package__:
	from .gmail_service import (
		GMAIL_READONLY_SCOPE,
		GmailOAuthError,
		GmailReadOnlyService,
		GmailServiceError,
	)
else:
	from gmail_service import (
		GMAIL_READONLY_SCOPE,
		GmailOAuthError,
		GmailReadOnlyService,
		GmailServiceError,
	)


app = FastAPI(title="SupportMemory Gmail API")
_oauth_lock = Lock()
_active_oauth_attempt: tuple[str, Flow] | None = None
_gmail_credentials: Credentials | None = None


@app.middleware("http")
async def redact_oauth_callback_query(request, call_next):
	response = await call_next(request)
	if request.url.path == "/oauth2callback":
		request.scope["query_string"] = b""
	return response


@app.get("/health")
def health() -> dict[str, str]:
	return {"status": "ok"}


@app.get("/oauth2/start")
def start_oauth() -> RedirectResponse:
	global _active_oauth_attempt
	try:
		flow = Flow.from_client_config(
			GmailReadOnlyService.load_web_client_config(),
			scopes=[GMAIL_READONLY_SCOPE],
		)
		flow.redirect_uri = GmailReadOnlyService.gmail_redirect_uri()
		state = secrets.token_urlsafe(32)
		authorization_url, _ = flow.authorization_url(
			access_type="offline", prompt="consent", state=state
		)
	except (GmailServiceError, ValueError) as exc:
		raise HTTPException(status_code=500, detail=str(exc)) from exc
	except Exception as exc:
		raise HTTPException(
			status_code=500, detail="Could not prepare Gmail authorization."
		) from exc

	with _oauth_lock:
		_active_oauth_attempt = (state, flow)
	return RedirectResponse(authorization_url)


@app.get("/oauth2callback", response_class=PlainTextResponse)
def oauth_callback(
	state: str | None = None,
	code: str | None = None,
) -> PlainTextResponse:
	global _active_oauth_attempt, _gmail_credentials
	if not state:
		raise HTTPException(status_code=400, detail="Missing OAuth state.")
	with _oauth_lock:
		attempt = _active_oauth_attempt
		if attempt is None or state != attempt[0]:
			raise HTTPException(status_code=400, detail="Invalid OAuth state.")
		if not code:
			raise HTTPException(status_code=400, detail="Missing authorization code.")
		_active_oauth_attempt = None

	try:
		flow = attempt[1]
		flow.fetch_token(code=code)
		credentials = flow.credentials
		if not isinstance(credentials, Credentials):
			raise GmailOAuthError("Google returned no Gmail credentials.")
	except Exception as exc:
		raise HTTPException(
			status_code=400,
			detail="Gmail authorization failed. Verify the redirect URI and retry.",
		) from exc

	with _oauth_lock:
		_gmail_credentials = credentials
	return PlainTextResponse("Gmail authorization succeeded. You can close this page.")


@app.get("/gmail/messages")
def recent_gmail_messages(
	max_results: int = Query(default=5, ge=1, le=10),
) -> dict[str, list[dict[str, str]]]:
	with _oauth_lock:
		credentials = _gmail_credentials
	if credentials is None:
		raise HTTPException(
			status_code=401,
			detail="Gmail is not connected. Visit /oauth2/start first.",
		)
	try:
		gmail = GmailReadOnlyService(credentials=credentials)
		messages = gmail.list_recent_inbox_messages(max_results=max_results)
	except GmailServiceError as exc:
		raise HTTPException(status_code=502, detail=str(exc)) from exc
	return {"messages": [asdict(message) for message in messages]}