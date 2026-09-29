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

## Gmail read-only test

1. In Google Cloud Console, enable the Gmail API and create an OAuth client ID
	for a **Web application**. The existing Desktop client JSON is kept untouched
	for reference; it cannot be used with the Codespaces HTTPS callback.
	Configure the OAuth consent screen and add your Google account as a test user
	if the app is in testing mode.
2. Run `python backend/test_gmail.py` once to print the exact `OAuth callback:`
	URI for this Codespace. Add that full URI as an **Authorized redirect URI**
	for the Web application client. It has the form
	`https://<CODESPACE_NAME>-8000.<GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN>/oauth2callback`.
3. Download the Web application OAuth client JSON to
	`backend/credentials/gmail-oauth-web-client.json`. The first authenticated
	run saves the token as `backend/credentials/gmail-token.json`; both files and
	the existing Desktop client are under the Git-ignored credentials directory.
4. Ensure port `8000` is forwarded in the Codespaces **Ports** view. Keep it
	private; the Python callback server binds to `0.0.0.0` and Google redirects to
	the Codespaces HTTPS forwarded URL, never to localhost.
5. Install dependencies with `pip install -r requirements.txt`, then rerun
	`python backend/test_gmail.py` to complete browser authorization and read-only
	Gmail access.

The script requests only Gmail's `gmail.readonly` scope, reads at most one recent
inbox message, and prints a draft response without sending or modifying email.
On first run after configuring the Web client, open the OAuth URL printed in
the terminal, complete consent, and let Google redirect the browser to the
Codespaces HTTPS callback. The terminal waits for that callback and stores the
resulting token locally.