"""Typed models for Crawlab API responses.

These are intentionally plain stdlib dataclasses (no pydantic) to keep the
client dependency-light. They are ``slots=True``, so accessing an attribute
that does not exist — a typo, or a field Crawlab did not return — raises
``AttributeError`` instead of silently yielding ``None`` the way the raw
response dicts used to. Construct them with :meth:`from_api`, which reads the
raw API dict and fails loudly (``KeyError``) if a required field is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DataCollection:
    """A Crawlab data collection (a registered MongoDB collection of results).

    The identifier lives in the Crawlab API field ``_id`` (Mongo ObjectID) and
    is exposed here as :attr:`id`.
    """

    id: str
    name: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    dedup: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> DataCollection:
        """Build from a raw Crawlab API dict.

        Missing ``_id`` or ``name`` raises ``KeyError`` — a schema mismatch
        should fail loudly, not silently produce an object with ``None`` fields.
        """
        return cls(
            id=raw["_id"],
            name=raw["name"],
            fields=raw.get("fields") or [],
            dedup=raw.get("dedup"),
        )
