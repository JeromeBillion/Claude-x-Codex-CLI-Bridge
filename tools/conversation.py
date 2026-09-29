"""Persistent shared conversation and curated cross-provider handoff (VLI-160).

The desktop keeps ONE local, inspectable record of a workspace conversation
across both providers. Each provider's native session/thread IDs are kept
apart: an ID is only ever stored next to the provider that issued it, and a
handoff never offers one provider's ID to the other as a resume handle.

Crossing providers is a *handoff packet* the user curates: every item can be
edited or left out, secrets are redacted, the result is bounded, and the
final text the user edited is scanned again before it is sent. The packet is
recorded in the log exactly as sent. Nothing here reads provider transcripts,
consumer chat history or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterable
import uuid

PROVIDERS = ("claude", "codex")
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex"}
ENTRY_KINDS = frozenset({
    "user_message",   # what the user sent, and to which provider(s) under which mode
    "turn",           # one provider turn: text, tools, approvals, verdict, limits, errors
    "handoff",        # the exact packet sent across providers, with its redaction report
    "collaboration",  # joint-approval state: approved / needs_user_decision
    "user_decision",  # what the user chose when providers disagreed or failed
    "note",           # host notices (limit reached, provider unavailable, ...)
})
MAX_TURN_TEXT = 60_000  # per turn, in the log; the handoff is bounded separately
LOG_SCHEMA = 1


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
# Ordered: specific token shapes before generic assignments. Each match is
# replaced by "[REDACTED:<category>]"; only categories and counts are
# reported, never the matched text.
REDACTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,})")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._~+/\-]{16,}=*")),
    ("url_credentials", re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")),
    ("secret_assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|ACCESS_KEY|CLIENT_SECRET)[A-Z0-9_]*)"
        r"(\s*[:=]\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"',;]{4,})")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
)
# A local home path reveals the account name; it is rewritten, not removed.
HOME_PATH = re.compile(r"(?i)(\b[A-Z]:[\\/]Users[\\/]|(?<![\w.])/(?:Users|home)/)([^\\/\s:*?\"<>|]+)")


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted text, {category: count}). Never reports the secret itself."""
    counts: dict[str, int] = {}

    def replace(category: str):
        def inner(match: re.Match[str]) -> str:
            counts[category] = counts.get(category, 0) + 1
            if category == "url_credentials":
                return f"{match.group(1)}[REDACTED:{category}]@"
            if category == "secret_assignment":
                return f"{match.group(1)}{match.group(2)}[REDACTED:{category}]"
            return f"[REDACTED:{category}]"
        return inner

    for category, pattern in REDACTION_RULES:
        text = pattern.sub(replace(category), text)

    def home(match: re.Match[str]) -> str:
        counts["home_path"] = counts.get("home_path", 0) + 1
        return match.group(1) + "<user>"

    text = HOME_PATH.sub(home, text)
    return text, counts


def describe_redactions(counts: dict[str, int]) -> str:
    if not counts:
        return "No secrets, e-mail addresses or home-folder names found."
    parts = [f"{count} x {category.replace('_', ' ')}" for category, count in sorted(counts.items())]
    return "Redacted: " + ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Persistent conversation
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workspace_key(workspace: Path) -> str:
    """A folder name for the workspace that does not reveal its path."""
    return hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode("utf-8")).hexdigest()[:16]


@dataclass
class Entry:
    seq: int
    at: str
    kind: str
    data: dict[str, Any]
    provider: str | None = None
    # Only ever the native ID issued by `provider`; never another provider's.
    session_ref: str | None = None

    def to_json(self) -> str:
        return json.dumps({"v": LOG_SCHEMA, "seq": self.seq, "at": self.at, "kind": self.kind,
                           "provider": self.provider, "session_ref": self.session_ref,
                           "data": self.data}, ensure_ascii=False)


class ConversationLog:
    """Append-only JSONL conversation for one workspace, in the user's private app state.

    Loading tolerates a torn last line (a crash mid-write) and skips corrupt
    lines, counting them in `damaged_lines`, instead of refusing the history.
    """

    def __init__(self, path: Path, workspace_name: str = "") -> None:
        self.path = path
        self.workspace_name = workspace_name
        self.entries: list[Entry] = []
        self.damaged_lines = 0
        self._lock = threading.Lock()  # the UI thread and the turn worker both record
        self._load()

    # -- construction ------------------------------------------------------
    @classmethod
    def open_latest(cls, state_dir: Path, workspace: Path) -> "ConversationLog":
        folder = state_dir / "conversations" / workspace_key(workspace)
        existing = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
        return cls(existing[-1] if existing else folder / f"{uuid.uuid4()}.jsonl", workspace.name)

    @classmethod
    def start_new(cls, state_dir: Path, workspace: Path) -> "ConversationLog":
        folder = state_dir / "conversations" / workspace_key(workspace)
        log = cls(folder / f"{uuid.uuid4()}.jsonl", workspace.name)
        log.append("note", {"event": "conversation_started"})
        return log

    def _load(self) -> None:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return
        # Decode line by line: a crash can cut a multibyte character in half, and that
        # must cost one damaged line, not the whole history (Codex review of PR #20).
        for chunk in data.split(b"\n"):
            try:
                line = chunk.decode("utf-8")
            except UnicodeDecodeError:
                self.damaged_lines += 1
                continue
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                entry = Entry(int(raw["seq"]), str(raw["at"]), str(raw["kind"]), dict(raw["data"]),
                              raw.get("provider"), raw.get("session_ref"))
            except (ValueError, KeyError, TypeError):
                self.damaged_lines += 1
                continue
            if entry.kind in ENTRY_KINDS and (entry.provider is None or entry.provider in PROVIDERS):
                self.entries.append(entry)
            else:
                self.damaged_lines += 1

    # -- writing -----------------------------------------------------------
    def append(self, kind: str, data: dict[str, Any], *, provider: str | None = None,
               session_ref: str | None = None) -> Entry:
        if kind not in ENTRY_KINDS:
            raise ValueError("unknown conversation entry kind")
        if provider is not None and provider not in PROVIDERS:
            raise ValueError("unknown provider")
        if session_ref is not None and provider is None:
            raise ValueError("a native session ID must name the provider that issued it")
        with self._lock:
            entry = Entry((self.entries[-1].seq + 1) if self.entries else 1, _now(), kind, data,
                          provider, session_ref)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists()
            torn = False
            if not new_file and self.path.stat().st_size:
                with open(self.path, "rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    torn = existing.read(1) != b"\n"
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                # A crash can leave a final line without its newline; close it off so the
                # fragment stays one damaged line instead of swallowing this entry.
                handle.write(("\n" if torn else "") + entry.to_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if new_file and os.name != "nt":
                os.chmod(self.path, 0o600)
            self.entries.append(entry)
            return entry

    def record_user(self, text: str, *, mode: str, providers: Iterable[str]) -> Entry:
        return self.append("user_message", {"text": text, "mode": mode, "to": sorted(set(providers))})

    def record_turn(self, turn: Any, *, role: str = "answer") -> Entry:
        """Store one finished provider turn (a desktop_state.TurnRecord) as a single entry."""
        events = list(getattr(turn, "events", []))
        tools: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        signals: list[dict[str, Any]] = []
        session_ref = None
        for event in events:
            session_ref = event.session_ref or session_ref
            kind, data = event.kind, event.data
            if kind == "tool_started":
                tools.append({"tool": str(data.get("tool")), "id": data.get("tool_use_id"), "error": None})
            elif kind == "tool_finished":
                for tool in tools:
                    if tool["id"] == data.get("tool_use_id") and tool["error"] is None:
                        tool["error"] = bool(data.get("is_error"))
            elif kind == "approval_request":
                approvals.append({"request_id": str(data.get("request_id")),
                                  "tool": str(data.get("tool") or data.get("family")),
                                  "policy": data.get("policy"), "decision": None})
            elif kind == "approval_decision":
                for approval in approvals:
                    if approval["request_id"] == str(data.get("request_id")):
                        approval["decision"] = data.get("decision")
                        approval["by"] = data.get("by")
            elif kind in {"rate_limit", "notice", "runtime_error", "process_exited"}:
                signals.append({"kind": kind, **{k: v for k, v in data.items() if isinstance(v, (str, int, float, bool)) or v is None}})
        text = str(getattr(turn, "text", ""))
        return self.append("turn", {
            "role": role, "model": str(getattr(turn, "model", "")), "text": text[-MAX_TURN_TEXT:],
            "text_truncated": len(text) > MAX_TURN_TEXT, "ok": bool(getattr(turn, "ok", False)),
            "failure": getattr(turn, "failure", None), "tools": tools, "approvals": approvals,
            "signals": signals,
        }, provider=getattr(turn, "provider", None), session_ref=session_ref)

    # -- reading -----------------------------------------------------------
    def native_sessions(self) -> dict[str, list[str]]:
        """Each provider's own native IDs, in first-seen order. Never merged."""
        sessions: dict[str, list[str]] = {provider: [] for provider in PROVIDERS}
        for entry in self.entries:
            if entry.provider and entry.session_ref and entry.session_ref not in sessions[entry.provider]:
                sessions[entry.provider].append(entry.session_ref)
        return sessions

    def last_turn(self, provider: str | None = None) -> Entry | None:
        return next((e for e in reversed(self.entries)
                     if e.kind == "turn" and (provider is None or e.provider == provider)), None)

    def render(self, *, redacted: bool = False) -> str:
        """The whole conversation as readable text, for the inspector window or an export."""
        lines = [f"Conversation for {self.workspace_name or 'workspace'} ({len(self.entries)} entries)"]
        if self.damaged_lines:
            lines.append(f"Note: {self.damaged_lines} damaged line(s) in the log were skipped.")
        for provider, refs in self.native_sessions().items():
            if refs:
                lines.append(f"{PROVIDER_NAMES[provider]} native sessions (resumable only in {PROVIDER_NAMES[provider]}): "
                             + ", ".join(refs))
        for entry in self.entries:
            head = f"\n[{entry.seq}] {entry.at} {entry.kind}"
            if entry.provider:
                head += f" / {PROVIDER_NAMES[entry.provider]}"
            lines.append(head)
            data = entry.data
            if entry.kind == "user_message":
                lines.append(f"You ({data.get('mode')} -> {', '.join(data.get('to', []))}): {data.get('text', '')}")
            elif entry.kind == "turn":
                verdict = "ok" if data.get("ok") else f"failed: {data.get('failure')}"
                lines.append(f"{data.get('role')} on {data.get('model')} ({verdict})")
                for tool in data.get("tools", []):
                    lines.append(f"  tool {tool['tool']}{' (error)' if tool.get('error') else ''}")
                for approval in data.get("approvals", []):
                    lines.append(f"  approval {approval['tool']}: {approval.get('decision') or 'unanswered'}"
                                 f" ({approval.get('by') or approval.get('policy')})")
                for signal in data.get("signals", []):
                    lines.append(f"  {signal.get('kind')}: " + ", ".join(
                        f"{k}={v}" for k, v in signal.items() if k != "kind" and v is not None))
                lines.append(data.get("text", ""))
            elif entry.kind == "handoff":
                lines.append(f"Handoff {data.get('source')} -> {data.get('target')}; "
                             f"{describe_redactions(data.get('redactions', {}))}")
                lines.append(data.get("packet", ""))
            else:
                lines.append(json.dumps(data, ensure_ascii=False))
        text = "\n".join(lines)
        return redact(text)[0] if redacted else text

    def forget(self) -> None:
        """Delete this conversation's local record (the user asked to)."""
        self.path.unlink(missing_ok=True)
        self.entries.clear()


# ---------------------------------------------------------------------------
# Curated handoff
# ---------------------------------------------------------------------------
@dataclass
class HandoffItem:
    id: str
    label: str
    text: str
    included: bool = True
    required: bool = False  # the user summary cannot be omitted


@dataclass
class HandoffDraft:
    source: str
    target: str
    workspace_name: str
    source_sessions: list[str]
    items: list[HandoffItem] = field(default_factory=list)
    reason: str = "switching provider"
    limit_chars: int = 8_000
    rendered_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in PROVIDERS or self.target not in PROVIDERS or self.source == self.target:
            raise ValueError("a handoff goes from one provider to the other")

    def item(self, item_id: str) -> HandoffItem:
        for candidate in self.items:
            if candidate.id == item_id:
                return candidate
        raise KeyError(item_id)

    def edit(self, item_id: str, text: str) -> None:
        self.item(item_id).text = text

    def set_included(self, item_id: str, included: bool) -> None:
        item = self.item(item_id)
        if item.required and not included:
            raise ValueError("the summary is required")
        item.included = included

    def render(self) -> tuple[str, dict[str, int]]:
        """The packet the user reviews: included items only, redacted, bounded."""
        source, target = PROVIDER_NAMES[self.source], PROVIDER_NAMES[self.target]
        header = [
            f"# Handoff from {source} to {target}",
            f"Workspace: {self.workspace_name}",
            f"Reason: {self.reason}",
            f"{source} native session(s): {', '.join(self.source_sessions) or 'none'} "
            f"(these resume only in {source}; this is a new {target} conversation, not a session transfer)",
        ]
        body: list[str] = []
        for item in self.items:
            if item.included and item.text.strip():
                body += ["", f"## {item.label}", item.text.strip()]
        omitted = [item.label for item in self.items if not item.included]
        if omitted:
            body += ["", "Left out by the user: " + ", ".join(omitted)]
        text, counts = redact("\n".join(header + body))
        if len(text) > self.limit_chars:
            marker = f"\n\n[Handoff shortened to {self.limit_chars} characters; edit or omit items to fit.]"
            text = text[:self.limit_chars - len(marker)] + marker
            counts = {**counts, "shortened": 1}
        self.rendered_counts = dict(counts)
        return text, counts

    def finalize(self, edited_packet: str, *, send_despite_findings: bool = False) -> tuple[str, dict[str, int]]:
        """Re-scan the text the user edited. Refuse if it reintroduced secrets, unless the user insists."""
        clean, found = redact(edited_packet)
        if found and not send_despite_findings:
            raise HandoffNeedsReview(found)
        final = edited_packet if send_despite_findings else clean
        if len(final) > self.limit_chars * 2:
            raise ValueError("handoff too long")
        # The report covers what render() already removed plus anything found in the user's edit.
        merged = dict(self.rendered_counts)
        for category, count in found.items():
            merged[category] = merged.get(category, 0) + count
        if send_despite_findings and found:
            merged["sent_unredacted_by_user"] = sum(found.values())
        return final, merged

    def record(self, log: ConversationLog, packet: str, counts: dict[str, int]) -> Entry:
        return log.append("handoff", {
            "source": self.source, "target": self.target, "reason": self.reason, "packet": packet,
            "redactions": counts, "included": [i.id for i in self.items if i.included],
            "omitted": [i.id for i in self.items if not i.included],
        })


class HandoffNeedsReview(ValueError):
    """The edited packet still contains something that looks secret."""

    def __init__(self, counts: dict[str, int]) -> None:
        super().__init__(describe_redactions(counts))
        self.counts = counts


def _limit_reason(turn: Entry | None) -> str | None:
    if turn is None:
        return None
    for signal in turn.data.get("signals", []):
        if signal.get("kind") == "rate_limit" and signal.get("status") == "rejected":
            return "the source provider reached its usage limit"
        if signal.get("kind") == "runtime_error" and signal.get("category") == "limit":
            return "the source provider reached its usage limit"
    if not turn.data.get("ok"):
        return f"the source provider's last turn failed ({turn.data.get('failure')})"
    return None


def draft_handoff(log: ConversationLog, *, source: str, target: str, user_summary: str,
                  selected_files: Iterable[str] = (), file_diffs: dict[str, str] | None = None,
                  reason: str | None = None, limit_chars: int = 8_000) -> HandoffDraft:
    """Build the default draft from the shared conversation. The user edits it before anything is sent."""
    last = log.last_turn(source)
    goal = next((e.data.get("text", "") for e in reversed(log.entries) if e.kind == "user_message"), "")
    draft = HandoffDraft(source, target, log.workspace_name, log.native_sessions()[source],
                         reason=reason or _limit_reason(last) or "switching provider", limit_chars=limit_chars)
    draft.items.append(HandoffItem("summary", "Summary from the user", user_summary, required=True))
    if goal:
        draft.items.append(HandoffItem("goal", "Latest user request", goal))
    if last is not None:
        data = last.data
        verdict = "completed" if data.get("ok") else f"failed or incomplete ({data.get('failure')})"
        draft.items.append(HandoffItem("verdict", f"{PROVIDER_NAMES[source]}'s last turn",
                                       f"{data.get('role')} on {data.get('model')}: {verdict}"))
        tools = sorted({t["tool"] for t in data.get("tools", [])})
        if tools:
            draft.items.append(HandoffItem("tools", "Tools it used", ", ".join(tools)))
        decisions = [f"{a['tool']}: {a.get('decision') or 'unanswered'}" for a in data.get("approvals", [])]
        if decisions:
            draft.items.append(HandoffItem("approvals", "Approval decisions", "; ".join(decisions)))
        if data.get("text"):
            draft.items.append(HandoffItem("reply", f"{PROVIDER_NAMES[source]}'s reply (tail)",
                                           data["text"][-max(500, limit_chars // 2):]))
    files = list(selected_files)
    if files:
        draft.items.append(HandoffItem("files", "Files the user selected", ", ".join(files)))
    for name, diff in (file_diffs or {}).items():
        draft.items.append(HandoffItem(f"diff:{name}", f"Diff of {name}", diff, included=False))
    return draft


__all__ = [
    "ConversationLog", "Entry", "HandoffDraft", "HandoffItem", "HandoffNeedsReview", "PROVIDERS",
    "describe_redactions", "draft_handoff", "redact", "workspace_key",
]
