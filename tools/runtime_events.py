"""Shared UI event contract for the personal Claude/Codex desktop host."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Envelope:
    provider: str
    # Always the provider's native session/thread id. Never a translated id.
    session_ref: str | None
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
