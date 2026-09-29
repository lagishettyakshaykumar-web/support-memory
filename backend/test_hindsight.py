"""Manual integration test for the Hindsight customer memory layer."""

from __future__ import annotations

import sys
import time

from hindsight_service import HindsightMemoryService, HindsightServiceError


CUSTOMER_NAME = "Ravi Kumar"
FACTS = (
    "Ravi prefers Telugu.",
    "Ravi has experienced recurring internet disconnection.",
    "Ravi previously contacted support about the same internet disconnection issue.",
    "Support previously asked Ravi to restart his router.",
    "The internet disconnection problem returned after the router restart.",
    "Ravi contacted support again about the recurring internet disconnection problem.",
)
RECALL_QUERY = (
    "What previous internet problems has Ravi Kumar experienced and "
    "what solutions were already tried?"
)
REFLECT_QUERY = (
    "Based on Ravi's previous support history, how should an agent respond to "
    "his latest internet disconnection complaint?"
)
RECALL_ATTEMPTS = 6
RECALL_RETRY_INTERVAL_SECONDS = 2


def main() -> int:
    service = None
    try:
        service = HindsightMemoryService()
        service.ensure_bank()
        print(f"Using Hindsight bank: {service.bank_id}")

        print("\nRetaining Ravi Kumar's support history:")
        for index, fact in enumerate(FACTS, start=1):
            result = service.retain_customer_information(CUSTOMER_NAME, fact)
            print(f"  {index}. Stored ({result.items_count} item(s)): {fact}")

        print(f"\nRecall query: {RECALL_QUERY}")
        recall = None
        for attempt in range(RECALL_ATTEMPTS):
            recall = service.recall_customer_memories(CUSTOMER_NAME, RECALL_QUERY)
            if recall.results or attempt == RECALL_ATTEMPTS - 1:
                break
            print("  Memories are still indexing; retrying shortly...")
            time.sleep(RECALL_RETRY_INTERVAL_SECONDS)

        assert recall is not None
        if recall.results:
            for index, memory in enumerate(recall.results, start=1):
                kind = f" [{memory.type}]" if memory.type else ""
                print(f"  {index}.{kind} {memory.text}")
        else:
            print("  No matching memories were returned.")

        if service.supports_reflect:
            print(f"\nReflection query: {REFLECT_QUERY}")
            recalled_context = "\n".join(
                dict.fromkeys(memory.text for memory in recall.results)
            )
            reflection = service.reflect_on_customer_memories(
                CUSTOMER_NAME, REFLECT_QUERY, context=recalled_context or None
            )
            print(reflection.text)
        else:
            print("\nReflection skipped: this installed SDK does not support reflect.")
        return 0
    except (HindsightServiceError, ValueError) as exc:
        print(f"Hindsight integration test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            try:
                service.close()
            except HindsightServiceError as exc:
                print(f"Warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())