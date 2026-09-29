"""Integration test for Hindsight-backed Groq support response generation."""

from __future__ import annotations

import sys

from agent import (
	GroqGenerationError,
	HindsightRecallError,
	MissingGroqAPIKeyError,
	SupportAgent,
	SupportAgentError,
)
from hindsight_service import HindsightServiceError


CUSTOMER_NAME = "Ravi Kumar"
CUSTOMER_MESSAGE = (
	"My internet is disconnecting again. I already restarted the router like "
	"support told me last time, but the problem came back."
)


def main() -> int:
	agent = None
	try:
		agent = SupportAgent()
		reply = agent.generate_response(CUSTOMER_NAME, CUSTOMER_MESSAGE)
		print(f"Customer: {reply.customer_name}")
		print(f"Latest message: {CUSTOMER_MESSAGE}")
		print("\nRetrieved Hindsight memories:")
		if reply.memories:
			for index, memory in enumerate(reply.memories, start=1):
				print(f"  {index}. {memory}")
		else:
			print("  No relevant customer memories found; response uses the latest message only.")
		print("\nAI-generated support response:")
		print(reply.response)
		return 0
	except MissingGroqAPIKeyError as exc:
		print(f"Configuration error: {exc}", file=sys.stderr)
		return 1
	except (GroqGenerationError, HindsightRecallError, HindsightServiceError, ValueError) as exc:
		print(f"Support agent test failed: {exc}", file=sys.stderr)
		return 1
	finally:
		if agent is not None:
			try:
				agent.close()
			except SupportAgentError as exc:
				print(f"Warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
	raise SystemExit(main())