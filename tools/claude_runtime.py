#!/usr/bin/env python3
"""Claude Code desktop runtime adapter: first vertical slice (VLI-158/159).

Drives the user's own installed, signed-in `claude` CLI as a long-lived
`--print` process speaking stream-json, and maps its events onto the
provider-neutral envelope the desktop UI renders (see
docs/claude-desktop-runtime.md). This is a local personal tool: it never
reads or stores credentials, never sets an API key, and refuses to start a
session unless the CLI reports a first-party Claude subscription login.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from claude_runtime_probe import (
    ProbeRefused,
    SUBSCRIPTION_PLANS,
    VERSION,
    child_env,
    initialize,
    summarize_account,
    summarize_auth,
    summarize_models,
    summarize_notice,
    summarize_rate_limit,
    summarize_result,
    require_subscription,
)


PROVIDER = "claude"
# Oldest CLI with every flag this adapter passes (`--permission-prompts`, Opus 5.5).
MIN_VERSION = (2, 1, 280)
# Newest CLI whose stream-json shapes were observed; newer runs but is flagged.
TESTED_VERSION = (2, 1, 283)
# Models that can bill usage credits with no consent prompt in -p mode.
CREDIT_BILLED = re.compile(r"fable|best", re.IGNORECASE)
EVENT_TIMEOUT_SECONDS = 600


class RuntimeRefused(RuntimeError):
    """The adapter refused to start or act, with a machine-readable reason."""


@dataclass(frozen=True)
class Envelope:
    """One provider-neutral UI event. `data` never holds credentials."""

    provider: str
    session_ref: str | None
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Preflight:
    executable: list[str]
    version: tuple[int, int, int] | None
    version_status: str  # "tested" | "newer_untested" | "too_old" | "unknown"
    auth: dict[str, Any]
    account: dict[str, Any]
    models: list[dict[str, Any]]


def discover_claude(env: dict[str, str] | None = None) -> list[str]:
    """Find the user's installed CLI: PATH first, then the documented install spots."""
    env = dict(os.environ if env is None else env)
    found = shutil.which("claude", path=env.get("PATH"))
    if found:
        return [found]
    profile = env.get("USERPROFILE") or env.get("HOME")
    candidates = []
    if profile:
        candidates.append(Path(profile) / ".local" / "bin" / "claude.exe")
    if env.get("APPDATA"):
        candidates.append(Path(env["APPDATA"]) / "npm" / "claude.cmd")
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    raise RuntimeRefused("claude_not_installed")


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION.search(text or "")
    return tuple(int(part) for part in match.group(0).split(".")) if match else None  # type: ignore[return-value]


def version_status(version: tuple[int, int, int] | None) -> str:
    if version is None:
        return "unknown"
    if version < MIN_VERSION:
        return "too_old"
    return "tested" if version <= TESTED_VERSION else "newer_untested"


def _run(executable: Sequence[str], arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*executable, *arguments], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env(),
                          timeout=30, check=False)


class TrustStore:
    """Workspaces the user explicitly trusted in the desktop app.

    `claude -p` skips the CLI's own trust dialog and runs a repository's hooks
    and `.mcp.json` servers, so the app must ask before the first session.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, bool]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _key(workspace: Path) -> str:
        return os.path.normcase(str(workspace.resolve()))

    def is_trusted(self, workspace: Path) -> bool:
        return self._load().get(self._key(workspace)) is True

    def trust(self, workspace: Path) -> None:
        data = self._load()
        data[self._key(workspace)] = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def normalize(event: dict[str, Any], session_ref: str | None) -> list[Envelope]:
    """Map one stream-json line to zero or more UI envelopes."""
    def env(kind: str, **data: Any) -> Envelope:
        return Envelope(PROVIDER, session_ref, kind, data)

    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event") or {}
        delta = inner.get("delta") or {}
        if inner.get("type") == "content_block_delta":
            if delta.get("type") == "text_delta":
                return [env("text_delta", text=delta.get("text", ""))]
            if delta.get("type") == "thinking_delta":
                return [env("thinking_delta")]
        return []
    if kind == "assistant":
        return [env("tool_started", tool=block.get("name"), tool_use_id=block.get("id"))
                for block in (event.get("message") or {}).get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"]
    if kind == "user":
        content = (event.get("message") or {}).get("content")
        return [env("tool_finished", tool_use_id=block.get("tool_use_id"),
                    is_error=block.get("is_error") is True)
                for block in content if isinstance(block, dict) and block.get("type") == "tool_result"] \
            if isinstance(content, list) else []
    if kind == "control_request" and (event.get("request") or {}).get("subtype") == "can_use_tool":
        request = event["request"]
        return [env("approval_request", request_id=event.get("request_id"),
                    tool=request.get("tool_name"), input=request.get("input") or {})]
    if kind == "rate_limit_event":
        return [env("rate_limit", **summarize_rate_limit(event.get("rate_limit_info")))]
    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            return [env("session_started", model=event.get("model"),
                        permission_mode=event.get("permissionMode"),
                        api_key_source=event.get("apiKeySource"))]
        if subtype in ("model_refusal_fallback", "api_retry"):
            return [env("notice", **summarize_notice(event))]
        return []
    if kind == "result":
        # Exit code and `subtype` are not the verdict: `is_error` is.
        return [env("turn_finished", **summarize_result(event))]
    return []


class ClaudeSession:
    """One chat: a long-lived `claude --print` stream-json process."""

    def __init__(self, preflight: Preflight, workspace: Path, *, model: str,
                 session_id: str | None = None, resume: str | None = None,
                 trust: TrustStore, allow_credit_models: bool = False,
                 spawn: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen) -> None:
        if not trust.is_trusted(workspace):
            raise RuntimeRefused("workspace_not_trusted")
        self._check_model(model, allow_credit_models)
        self.allow_credit_models = allow_credit_models
        self.session_ref = resume or session_id
        command = [
            *preflight.executable, "--print",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--permission-prompt-tool", "stdio", "--permission-mode", "default",
            "--model", model,
        ]
        if resume:
            command += ["--resume", resume]
        elif session_id:
            command += ["--session-id", session_id]
        self.process = spawn(command, cwd=workspace, env=child_env(), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             encoding="utf-8", errors="replace")
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._pending_approvals: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _check_model(model: str, allow_credit_models: bool) -> None:
        if CREDIT_BILLED.search(model) and not allow_credit_models:
            raise RuntimeRefused("model_may_bill_usage_credits")

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._events.put(event)
        self._events.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _control(self, request_id: str, request: dict[str, Any]) -> None:
        self._send({"type": "control_request", "request_id": request_id, "request": request})

    def send(self, text: str) -> None:
        self._send({"type": "user", "message": {"role": "user", "content": text}})

    def events(self) -> Iterator[Envelope]:
        """Yield envelopes until the current turn finishes or the process exits."""
        while True:
            try:
                raw = self._events.get(timeout=EVENT_TIMEOUT_SECONDS)
            except queue.Empty:
                yield Envelope(PROVIDER, self.session_ref, "turn_finished",
                               {"ok": False, "subtype": "host_timeout"})
                return
            if raw is None:
                yield Envelope(PROVIDER, self.session_ref, "process_exited",
                               {"exit_code": self.process.poll()})
                return
            if raw.get("session_id"):
                self.session_ref = raw["session_id"]
            for envelope in normalize(raw, self.session_ref):
                if envelope.kind == "approval_request":
                    self._pending_approvals[str(envelope.data["request_id"])] = envelope.data["input"]
                yield envelope
                if envelope.kind == "turn_finished":
                    return

    def answer_approval(self, request_id: str, allow: bool, *,
                        tool_input: dict[str, Any] | None = None, message: str = "Denied") -> None:
        if request_id not in self._pending_approvals:
            raise RuntimeRefused("unknown_approval_request")
        proposed = self._pending_approvals.pop(request_id)
        # Allow runs exactly what the user saw unless the UI passes an edited input.
        answer = ({"behavior": "allow", "updatedInput": proposed if tool_input is None else tool_input}
                  if allow
                  else {"behavior": "deny", "message": message})
        self._send({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": answer}})

    def set_model(self, model: str) -> None:
        self._check_model(model, self.allow_credit_models)
        self._control("set_model", {"subtype": "set_model", "model": model})

    def interrupt(self) -> None:
        self._control("interrupt", {"subtype": "interrupt"})

    def close(self) -> int | None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            return self.process.wait(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()
            return None
        finally:
            self._reader.join(timeout=5)
            if self.process.stdout and not self._reader.is_alive():
                self.process.stdout.close()


def preflight(executable: Sequence[str] | None = None) -> Preflight:
    """Version, auth and plan checks before any session; makes no model call."""
    exe = list(executable) if executable else discover_claude()
    version = parse_version(_run(exe, ["--version"]).stdout)
    status = version_status(version)
    if status == "too_old":
        raise RuntimeRefused("cli_too_old")
    auth_run = _run(exe, ["auth", "status", "--json"])
    try:
        raw_auth = json.loads(auth_run.stdout)
    except json.JSONDecodeError:
        raw_auth = None
    auth = summarize_auth(raw_auth, auth_run.returncode)
    probe = subprocess.Popen([*exe, "--print", "--input-format", "stream-json",
                              "--output-format", "stream-json", "--verbose", "--tools", "",
                              "--no-session-persistence"],
                             env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                             errors="replace")
    try:
        init = initialize(_LineSession(probe))
    finally:
        if probe.stdin:
            probe.stdin.close()
        try:
            probe.wait(timeout=30)
        except subprocess.TimeoutExpired:
            probe.kill()
        if probe.stdout:
            probe.stdout.close()
    account = summarize_account(init.get("account"))
    try:
        require_subscription(auth, account)
    except ProbeRefused as refusal:
        raise RuntimeRefused(str(refusal)) from None
    return Preflight(exe, version, status, auth, account, summarize_models(init.get("models")))


class _LineSession:
    """Minimal synchronous adapter so `initialize` can drive a raw process."""

    def __init__(self, process: "subprocess.Popen[str]") -> None:
        self.process = process

    def control(self, request_id: str, request: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"type": "control_request", "request_id": request_id,
                                             "request": request}) + "\n")
        self.process.stdin.flush()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def build_handoff(workspace: Path, envelopes: Sequence[Envelope], *, user_summary: str,
                  files: Sequence[str] = (), limit_chars: int = 8_000) -> str:
    """An explicit, user-visible context packet for starting a Codex thread.

    Native Claude sessions are not portable to Codex; this is the only thing
    that crosses providers, and the user sees and can edit it before it goes.
    """
    reply = "".join(e.data.get("text", "") for e in envelopes if e.kind == "text_delta")
    tools = sorted({str(e.data.get("tool")) for e in envelopes if e.kind == "tool_started"})
    last = next((e for e in reversed(envelopes) if e.kind == "turn_finished"), None)
    lines = [
        "# Handoff from Claude Code",
        f"Workspace: {workspace.name}",
        f"Claude session: {envelopes[-1].session_ref if envelopes else 'none'} (not resumable in Codex)",
        f"Last turn ok: {bool(last and last.data.get('ok'))}",
        f"Tools used: {', '.join(tools) or 'none'}",
        "Files the user selected: " + (", ".join(files) or "none"),
        "",
        "## User summary",
        user_summary.strip(),
        "",
        "## Claude's last reply (tail)",
        reply[-max(0, limit_chars // 2):].strip(),
    ]
    return "\n".join(lines)[:limit_chars]


__all__ = [
    "ClaudeSession", "Envelope", "Preflight", "RuntimeRefused", "SUBSCRIPTION_PLANS",
    "TrustStore", "build_handoff", "discover_claude", "normalize", "parse_version",
    "preflight", "version_status",
]
