"""Provider-neutral state for the local desktop timeline.

No provider session is translated into another provider's native history.
The user reviews the handoff text before it is sent across that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Literal

from tools.runtime_events import Envelope

Mode = Literal["gpt_only", "collaboration", "claude_only"]
MODES: tuple[Mode, ...] = ("gpt_only", "collaboration", "claude_only")
BLOCKING_KINDS = frozenset({"approval_request", "mcp_elicitation", "connector_approval_request"})


@dataclass(frozen=True)
class RolePlan:
    lead: str
    final: str
    reason: str


def parse_role_plan(text: str) -> RolePlan:
    """A role nomination has no authority unless its entire output is valid JSON."""
    if not isinstance(text, str) or len(text) > 2048:
        raise ValueError("Role nomination is too long")
    candidate = text.strip()
    if candidate.startswith("```json\n") and candidate.endswith("\n```"):
        candidate = candidate[8:-4].strip()
    try:
        def unique_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Role nomination has a duplicate field")
                value[key] = item
            return value
        raw = json.loads(candidate, object_pairs_hook=unique_pairs)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Role nomination is not exact JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"lead", "final", "reason"}:
        raise ValueError("Role nomination has an unexpected shape")
    if not isinstance(raw["lead"], str) or not isinstance(raw["final"], str) \
            or raw["lead"] not in {"codex", "claude"} or raw["final"] not in {"codex", "claude"}:
        raise ValueError("Role nomination names an unknown provider")
    reason = raw["reason"]
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError("Role nomination needs a short reason")
    return RolePlan(raw["lead"], raw["final"], reason.strip())


@dataclass
class TurnRecord:
    provider: str
    model: str
    events: list[Envelope] = field(default_factory=list)
    text: str = ""
    finished: bool = False
    ok: bool = False
    failure: str | None = None

    def append(self, event: Envelope) -> None:
        if event.provider != self.provider:
            raise ValueError("Event provider differs from turn owner")
        self.events.append(event)
        if event.kind == "text_delta":
            self.text += str(event.data.get("text", ""))
        elif event.kind == "turn_finished":
            self.finished = True
            self.ok = event.data.get("ok") is True
            if not self.ok:
                self.failure = str(event.data.get("terminal_reason") or event.data.get("subtype") or "failed")
        elif event.kind in {"process_exited", "runtime_error"}:
            self.finished = True
            self.ok = False
            self.failure = str(event.data.get("category") or event.kind)


@dataclass
class Collaboration:
    """A result is joint only after two explicit votes on identical content."""

    candidate: str = ""
    source_provider: str = ""
    final_provider: str = ""
    votes: dict[str, tuple[bool, str]] = field(default_factory=dict)
    state: str = "draft"  # draft | awaiting_review | approved | needs_user_decision

    def propose(self, candidate: str, source_provider: str) -> None:
        if not candidate.strip():
            raise ValueError("Empty collaboration candidate")
        if source_provider not in {"codex", "claude"}:
            raise ValueError("Unknown collaboration lead")
        self.candidate = candidate
        self.source_provider = source_provider
        self.votes.clear()
        self.state = "awaiting_review"

    def vote(self, provider: str, *, approves: bool, reviewed_text: str) -> None:
        if self.state != "awaiting_review" or provider not in {"codex", "claude"}:
            raise ValueError("No pending collaboration review for this provider")
        if provider in self.votes:
            raise ValueError("Provider already voted")
        # A provider may not approve a different answer than the one displayed.
        valid = approves and reviewed_text == self.candidate
        self.votes[provider] = (valid, reviewed_text)
        if not valid:
            self.state = "needs_user_decision"
        elif len(self.votes) == 2:
            self.state = "approved"

    def unavailable(self) -> None:
        self.state = "needs_user_decision"

    @property
    def joint_answer(self) -> str | None:
        return self.candidate if self.state == "approved" else None


def handoff_text(workspace_name: str, source: TurnRecord, user_summary: str,
                 selected_files: tuple[str, ...] = ()) -> str:
    """Small inspectable packet; caller displays an editor before routing it."""
    if not source.finished:
        raise ValueError("Cannot hand off an active turn")
    if not user_summary.strip():
        raise ValueError("A handoff summary is required")
    native_ref = source.events[-1].session_ref if source.events else None
    return "\n".join((
        f"Workspace: {workspace_name}",
        f"Source: {source.provider} / {source.model}",
        f"Native source session: {native_ref or 'none'} (cannot resume in the other provider)",
        f"Source turn: {'completed' if source.ok else 'failed or incomplete'}",
        "Selected files: " + (", ".join(selected_files) or "none"),
        "", "User summary:", user_summary.strip(), "", "Source output:", source.text[-4000:],
    ))[:8000]
