"""Hindsight memory service for customer support history."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from hindsight_client import Hindsight


DEFAULT_BASE_URL = "https://api.hindsight.vectorize.io"
DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
REFLECT_MISSION = (
	"Help support agents respond with continuity by considering the customer's "
	"previous support history and solutions already attempted."
)


class HindsightServiceError(RuntimeError):
	"""Raised when Hindsight configuration or an API operation fails."""


@dataclass(frozen=True)
class HindsightSettings:
	api_key: str
	base_url: str
	bank_id: str

	@classmethod
	def from_env(cls) -> HindsightSettings:
		load_dotenv(DOTENV_PATH, override=False)
		api_key = os.getenv("HINDSIGHT_API_KEY", "").strip()
		bank_id = os.getenv("HINDSIGHT_BANK_ID", "").strip()
		base_url = os.getenv("HINDSIGHT_BASE_URL", DEFAULT_BASE_URL).strip()

		missing = [
			key
			for key, value in (
				("HINDSIGHT_API_KEY", api_key),
				("HINDSIGHT_BANK_ID", bank_id),
			)
			if not value
		]
		if missing:
			raise HindsightServiceError(
				"Missing required environment variable(s): " + ", ".join(missing)
			)
		if not base_url:
			base_url = DEFAULT_BASE_URL

		return cls(api_key=api_key, base_url=base_url.rstrip("/"), bank_id=bank_id)


class HindsightMemoryService:
	"""Small synchronous wrapper around the official Hindsight Python client."""

	def __init__(self, settings: HindsightSettings | None = None) -> None:
		self.settings = settings or HindsightSettings.from_env()
		self.bank_id = self.settings.bank_id
		try:
			self.client = Hindsight(
				base_url=self.settings.base_url,
				api_key=self.settings.api_key,
			)
		except Exception as exc:
			raise HindsightServiceError("Could not initialize the Hindsight client") from exc

	def ensure_bank(self) -> Any:
		"""Create or update the configured bank; the SDK operation is idempotent."""
		try:
			return self.client.create_bank(
				bank_id=self.bank_id,
				reflect_mission=REFLECT_MISSION,
			)
		except Exception as exc:
			raise HindsightServiceError(
				f"Could not create or verify Hindsight bank '{self.bank_id}'"
			) from exc

	@staticmethod
	def _customer_tag(customer_name: str) -> str:
		slug = re.sub(r"[^a-z0-9]+", "-", customer_name.casefold()).strip("-")
		if not slug:
			raise ValueError("customer_name must contain letters or numbers")
		return f"customer-{slug}"

	def retain_customer_information(
		self, customer_name: str, information: str
	) -> Any:
		"""Retain one customer fact, tagged for customer-specific retrieval."""
		customer_name = customer_name.strip()
		information = information.strip()
		if not customer_name or not information:
			raise ValueError("customer_name and information must not be empty")

		try:
			response = self.client.retain(
				bank_id=self.bank_id,
				content=f"{customer_name}: {information}",
				metadata={"customer_name": customer_name},
				tags=[self._customer_tag(customer_name)],
			)
		except Exception as exc:
			raise HindsightServiceError(
				f"Could not retain information for '{customer_name}'"
			) from exc

		if not response.success:
			raise HindsightServiceError(
				f"Hindsight did not confirm retaining information for '{customer_name}'"
			)
		return response

	def recall_customer_memories(self, customer_name: str, query: str) -> Any:
		"""Recall relevant memories scoped to a single customer."""
		if not query.strip():
			raise ValueError("query must not be empty")
		try:
			return self.client.recall(
				bank_id=self.bank_id,
				query=query,
				tags=[self._customer_tag(customer_name)],
				tags_match="all_strict",
			)
		except Exception as exc:
			raise HindsightServiceError(
				f"Could not recall memories for '{customer_name}'"
			) from exc

	def reflect_on_customer_memories(
		self, customer_name: str, query: str, context: str | None = None
	) -> Any:
		"""Generate a contextual answer from the customer's stored memories."""
		if not query.strip():
			raise ValueError("query must not be empty")
		reflect = getattr(self.client, "reflect", None)
		if not callable(reflect):
			raise HindsightServiceError(
				"The installed Hindsight SDK does not support reflect"
			)
		try:
			return reflect(
				bank_id=self.bank_id,
				query=query,
				context=context,
				tags=[self._customer_tag(customer_name)],
				tags_match="all_strict",
			)
		except Exception as exc:
			raise HindsightServiceError(
				f"Could not reflect on memories for '{customer_name}'"
			) from exc

	@property
	def supports_reflect(self) -> bool:
		return callable(getattr(self.client, "reflect", None))

	def close(self) -> None:
		try:
			self.client.close()
		except Exception as exc:
			raise HindsightServiceError("Could not close the Hindsight client") from exc