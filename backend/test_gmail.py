"""Read one Gmail inbox message and draft a support response without sending it."""

from __future__ import annotations

import sys

from agent import SupportAgent, SupportAgentError
from gmail_service import (
	GmailReadOnlyService,
	GmailServiceError,
	truncate_body_for_display,
)


def main() -> int:
	agent = None
	try:
		print(f"OAuth callback: {GmailReadOnlyService.gmail_redirect_uri()}")
		gmail = GmailReadOnlyService()
		messages = gmail.list_recent_inbox_messages(max_results=1)
		if not messages:
			print("The Gmail inbox is empty; no response was generated.")
			return 0

		message = messages[0]
		print(f"Sender: {message.sender}")
		print(f"Subject: {message.subject}")
		print("Email body (truncated for display):")
		print(truncate_body_for_display(message.body))

		customer_message = (
			f"Sender: {message.sender}\n"
			f"Subject: {message.subject}\n\n"
			f"{message.body}"
		)
		agent = SupportAgent()
		reply = agent.generate_response(message.customer_name, customer_message)
		print("\nRetrieved Hindsight memories:")
		if reply.memories:
			for index, memory in enumerate(reply.memories, start=1):
				print(f"  {index}. {memory}")
		else:
			print("  No relevant customer history was found.")
		print("\nAI-generated support response (not sent):")
		print(reply.response)
		return 0
	except (GmailServiceError, SupportAgentError, ValueError) as exc:
		print(f"Gmail support test failed: {exc}", file=sys.stderr)
		return 1
	finally:
		if agent is not None:
			try:
				agent.close()
			except SupportAgentError as exc:
				print(f"Warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
	raise SystemExit(main())