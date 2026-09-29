# support-memory

## Project structure

```text
backend/
	main.py
	hindsight_service.py
	agent.py
	gmail_service.py
.env
.gitignore
requirements.txt
README.md
```

## Gmail read-only web service

Create a Google OAuth client ID for a **Web application**, enable the Gmail API,
and configure the consent screen. Add your Google account as a test user if the
consent screen is in testing mode. Configure the exact HTTPS
`/oauth2callback` URL as an authorized redirect URI.

Install dependencies with `pip install -r requirements.txt`. Set these service
environment variables in Render (or in an untracked local `.env` file):

- `GMAIL_OAUTH_CLIENT_JSON`: the complete Web application OAuth client JSON.
- `GMAIL_REDIRECT_URI`: the deployed HTTPS URL ending in `/oauth2callback`.
- `GROQ_API_KEY` and optionally `GROQ_MODEL`.
- `HINDSIGHT_API_KEY`, `HINDSIGHT_BANK_ID`, and optionally `HINDSIGHT_BASE_URL`.

Use this Render start command:

```sh
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

Visit `/oauth2/start` to connect Gmail, then use `/gmail/messages` to read up to
10 recent inbox messages. The service requests only the
`https://www.googleapis.com/auth/gmail.readonly` scope and keeps OAuth state and
credentials in memory for the lifetime of the running process. No email is
modified or sent. Do not commit `.env`, OAuth client JSON, or token files.