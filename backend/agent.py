"""Customer-support reasoning with Hindsight context and Groq generation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from groq import Groq

from hindsight_service import (
	DOTENV_PATH,
	HindsightMemoryService,
	HindsightServiceError,
)


DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
SYSTEM_PROMPT = """You are a helpful, concise customer-support agent.
Use the customer's latest message and the supplied Hindsight memories to answer
personally and accurately. Explicitly acknowledge relevant actions and problems
already in the history; never ask the customer to repeat information that is
already known. Do not recommend repeating a troubleshooting step that already
failed. If no relevant memories were found, do not imply that you have prior
history; use only the latest message. Be natural, empathetic, and customer-
friendly. Keep the response to a few short sentences. Do not claim that a ticket,
technician visit, refund, escalation, or other external action has occurred.
"""


class SupportAgentError(RuntimeError):
	"""Raised when memory-backed support response generation fails."""


class MissingGroqAPIKeyError(SupportAgentError):
	"""Raised when GROQ_API_KEY is not configured."""


class HindsightRecallError(SupportAgentError):
	"""Raised when customer history cannot be retrieved from Hindsight."""


class GroqGenerationError(SupportAgentError):
	"""Raised when Groq fails to generate a support response."""


@dataclass(frozen=True)
class SupportReply:
	customer_name: str
	memories: tuple[str, ...]
	response: str


class SupportAgent:
	"""Recall customer history, then use Groq to draft a personalized reply."""

	def __init__(
		self,
		memory_service: HindsightMemoryService | None = None,
		groq_client: Groq | None = None,
		api_key: str | None = None,
		model: str | None = None,
	) -> None:
		load_dotenv(DOTENV_PATH, override=False)
		configured_key = api_key or os.getenv("GROQ_API_KEY", "").strip()
		if groq_client is None and not configured_key:
			raise MissingGroqAPIKeyError(
				"GROQ_API_KEY is missing. Add it to the repository .env file."
			)

		self.model = (model or os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)).strip()
		if not self.model:
			self.model = DEFAULT_GROQ_MODEL
		try:
			self.groq_client = groq_client or Groq(api_key=configured_key)
		except Exception as exc:
			raise GroqGenerationError("Could not initialize the Groq client") from exc

		try:
			self.memory_service = memory_service or HindsightMemoryService()
		except HindsightServiceError as exc:
			raise HindsightRecallError("Could not initialize Hindsight memory access") from exc

	def generate_response(
		self, customer_name: str, customer_message: str
	) -> SupportReply:
		customer_name = customer_name.strip()
		customer_message = customer_message.strip()
		if not customer_name or not customer_message:
			raise ValueError("customer_name and customer_message must not be empty")

		try:
			recall = self.memory_service.recall_customer_memories(
				customer_name,
				f"Customer support history relevant to this message: {customer_message}",
			)
		except HindsightServiceError as exc:
			raise HindsightRecallError(
				f"Could not retrieve Hindsight memories for '{customer_name}'"
			) from exc

		memories = tuple(
			dict.fromkeys(
				memory.text.strip()
				for memory in recall.results
				if memory.text and memory.text.strip()
			)
		)
		memory_context = (
			"\n".join(f"- {memory}" for memory in memories)
			if memories
			else "No relevant Hindsight memories were found. Do not assume prior history."
		)
		user_prompt = (
			f"Customer: {customer_name}\n\n"
			f"Latest customer message:\n{customer_message}\n\n"
			f"Relevant Hindsight memories:\n{memory_context}\n\n"
			"Write the support response to the latest message."
		)

		try:
			completion = self.groq_client.chat.completions.create(
				model=self.model,
				messages=[
					{"role": "system", "content": SYSTEM_PROMPT},
					{"role": "user", "content": user_prompt},
				],
			)
		except Exception as exc:
			raise GroqGenerationError(
				f"Groq response generation failed using model '{self.model}'"
			) from exc

		if not completion.choices:
			raise GroqGenerationError("Groq returned no response choices")
		response = completion.choices[0].message.content
		if not response or not response.strip():
			raise GroqGenerationError("Groq returned an empty support response")

		return SupportReply(
			customer_name=customer_name,
			memories=memories,
			response=response.strip(),
		)

	def close(self) -> None:
		try:
			self.memory_service.close()
		except HindsightServiceError as exc:
			raise HindsightRecallError("Could not close Hindsight memory access") from exc
		close_groq = getattr(self.groq_client, "close", None)
		if callable(close_groq):
			close_groq()