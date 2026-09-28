#!/usr/bin/env python3
"""Claude Code desktop runtime adapter (VLI-158/159/160).

Drives the user's own installed, signed-in `claude` CLI as a long-lived
`--print` process speaking stream-json, and maps its events onto the
provider-neutral envelope the desktop timeline renders (see
docs/claude-desktop-runtime.md). This is a local personal tool: it never
reads or stores credentials, never sets an API key, and refuses to start a
session unless the CLI reports a first-party Claude subscription login, both
at preflight and again inside the chat process itself.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

# Works both as a flat script module (tools/ on sys.path, as the tests and
# `python tools\claude_runtime.py` use it) and as the package `tools.claude_runtime`.
try:
    from claude_runtime_probe import (
        ProbeRefused, SUBSCRIPTION_PLANS, VERSION, child_env, enum, initialize, require_subscription,
        summarize_account, summarize_auth, summarize_models, summarize_notice,
        summarize_rate_limit, summarize_result,
    )
except ImportError:  # imported as tools.claude_runtime from the repo root
    from tools.claude_runtime_probe import (
        ProbeRefused, SUBSCRIPTION_PLANS, VERSION, child_env, enum, initialize, require_subscription,
        summarize_account, summarize_auth, summarize_models, summarize_notice,
        summarize_rate_limit, summarize_result,
    )
# One Envelope class shared with the Codex adapter whenever the package is importable.
try:
    from tools.runtime_events import Envelope
    from tools.approval_modes import (
        APPROVAL_MODES, ASK_EVERY_EDIT, AUTO_ACCEPT_TRUSTED, CLAUDE_ASK_RULES, CLAUDE_FILE_EDIT_TOOLS,
        TrustedFolderStore, claude_auto_accept,
    )
except ImportError:
    from runtime_events import Envelope
    from approval_modes import (  # type: ignore[no-redef]
        APPROVAL_MODES, ASK_EVERY_EDIT, AUTO_ACCEPT_TRUSTED, CLAUDE_ASK_RULES, CLAUDE_FILE_EDIT_TOOLS,
        TrustedFolderStore, claude_auto_accept,
    )


PROVIDER = "claude"
# Chat sessions keep the user's development environment (JAVA_HOME, their own
# cloud CLIs, ...) but never anything that picks who bills Claude: every
# Anthropic variable, Claude Code token or provider switch, the Bedrock bearer
# token, Vertex region routing and NODE_OPTIONS code injection. Preflight and
# the probe use the stricter allowlist in `child_env`.
SESSION_ENV_BLOCKED_PREFIXES = (
    "ANTHROPIC_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "VERTEX_REGION_",
)
SESSION_ENV_BLOCKED = frozenset({"CLOUD_ML_REGION", "NODE_OPTIONS"})
# Oldest CLI seen with every flag this adapter passes and an `initialize` reply
# carrying the model menu: Jerome's Windows npm install, 2026-09-28. Older CLIs
# are refused because nothing about them has been observed.
MIN_VERSION = (2, 1, 201)
# The CLI whose full stream-json turn shapes were observed (turns, approvals,
# resume, interrupt). Every other version in range runs, labeled untested.
TESTED_VERSION = (2, 1, 283)
# Models that can bill usage credits with no consent prompt in -p mode.
CREDIT_BILLED = re.compile(r"fable|best", re.IGNORECASE)
# A menu value that is safe to put on a command line, including through a
# Windows .cmd shim: aliases, IDs and the "[1m]" context suffix.
MODEL_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,95}(\[1m\])?")
# Characters cmd.exe expands or re-quotes even inside a quoted argument.
CMD_UNSAFE = re.compile(r'[%"\r\n\x00&|<>^()!]')
# Native Claude session handles are UUIDs; nothing else goes on the command line or is adopted from events.
SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Assistant-message error codes, grouped into the shared runtime_error categories.
ASSISTANT_ERRORS = {
    "authentication_failed": "auth", "oauth_org_not_allowed": "auth", "billing_error": "billing",
    "rate_limit": "limit", "server_error": "connection", "invalid_request": "other",
    "max_output_tokens": "other", "unknown": "other",
}
# Tools that change files or run commands: they must reach the host for a decision.
HOST_DECIDED_TOOLS = frozenset(CLAUDE_ASK_RULES)
EVENT_TIMEOUT_SECONDS = 600
PREFLIGHT_TIMEOUT_SECONDS = 60
INITIALIZE_TIMEOUT_SECONDS = 60


class RuntimeRefused(RuntimeError):
    """The adapter refused to start or act, with a machine-readable reason."""


@dataclass
class Preflight:
    executable: list[str]
    version: tuple[int, int, int] | None
    version_status: str  # tested | older_untested | newer_untested | too_old | unknown
    auth: dict[str, Any]
    account: dict[str, Any]
    # The live, callable menu for this machine's UI (never published: see models_report).
    models: list[dict[str, Any]]
    # The redacted, shareable form of the same menu.
    models_report: list[dict[str, Any]]


class CreditConsent:
    """The user's explicit yes to credit-billed models (Fable) for ONE Claude session.

    Headless Fable can draw on usage credits without the CLI's own consent
    prompt, so the desktop host asks. A consent binds to the first session that
    uses it and cannot be reused by another session, a resumed process
    included: every new session asks again.
    """

    def __init__(self, *, confirmed_by_user: bool, model: str | None = None) -> None:
        if confirmed_by_user is not True:
            raise RuntimeRefused("credit_consent_not_confirmed")
        # The model named in the confirmation the user saw; a session may not start on another one.
        self.model = model
        self._owner: object | None = None

    def bind(self, owner: object) -> None:
        if self._owner is not None and self._owner is not owner:
            raise RuntimeRefused("credit_consent_already_used")
        self._owner = owner


def session_env(base: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if base is None else base)
    env = {name: value for name, value in source.items()
           if not name.upper().startswith(SESSION_ENV_BLOCKED_PREFIXES)
           and name.upper() not in SESSION_ENV_BLOCKED}
    env["DISABLE_AUTOUPDATER"] = "1"  # upgrades go through the version guard, not mid-session
    return env


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
    if version == TESTED_VERSION:
        return "tested"
    return "older_untested" if version < TESTED_VERSION else "newer_untested"


def is_credit_billed(*names: Any) -> bool:
    return any(isinstance(name, str) and CREDIT_BILLED.search(name) for name in names)


def local_menu(models: Any) -> list[dict[str, Any]]:
    """Every model the installed CLI offers, in a form that can be passed back to it.

    Kept on this machine for the model picker. Values that could not be passed
    safely on a command line are dropped (and counted by the redacted report).
    """
    menu = []
    for model in models if isinstance(models, list) else []:
        if not isinstance(model, dict):
            continue
        value = model.get("value")
        if not isinstance(value, str) or not MODEL_VALUE.fullmatch(value):
            continue
        resolved = model.get("resolvedModel")
        resolved = resolved if isinstance(resolved, str) and MODEL_VALUE.fullmatch(resolved) else None
        name = model.get("displayName")
        name = "".join(ch for ch in name if ch.isprintable())[:80] if isinstance(name, str) else ""
        levels = model.get("supportedEffortLevels")
        menu.append({
            "value": value,
            "resolved_model": resolved,
            "display_name": name or value,
            "effort_levels": [level for level in levels if level in ("low", "medium", "high", "xhigh", "max")]
            if isinstance(levels, list) else [],
            "credit_billed": is_credit_billed(value, resolved, name),
        })
    return menu[:100]


def _run(executable: Sequence[str], arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*executable, *arguments], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env(),
                          timeout=30, check=False)


def _is_cmd_shim(executable: Sequence[str]) -> bool:
    return bool(executable) and executable[0].lower().endswith((".cmd", ".bat"))


def _kill_tree(process: "subprocess.Popen[str]") -> None:
    """A .cmd shim's real child is node; killing only cmd.exe would orphan it."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                       check=False, timeout=30)
    else:
        process.kill()


class TrustStore:
    """Workspaces the user explicitly trusted to open a Claude session in.

    `claude -p` skips the CLI's own trust dialog and runs a repository's hooks
    and `.mcp.json` servers, so the app must ask before the first session.
    Trust for automatic edits is a separate grant (`TrustedFolderStore`).
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
        envelopes = [env("tool_started", tool=block.get("name"), tool_use_id=block.get("id"))
                     for block in (event.get("message") or {}).get("content") or []
                     if isinstance(block, dict) and block.get("type") == "tool_use"]
        if event.get("error") is not None:
            code = enum(event.get("error"), frozenset(ASSISTANT_ERRORS))
            envelopes.append(env("runtime_error", category=ASSISTANT_ERRORS.get(code or "", "other"),
                                 code=code))
        return envelopes
    if kind == "user":
        content = (event.get("message") or {}).get("content")
        return [env("tool_finished", tool_use_id=block.get("tool_use_id"),
                    is_error=block.get("is_error") is True)
                for block in content if isinstance(block, dict) and block.get("type") == "tool_result"] \
            if isinstance(content, list) else []
    if kind == "control_request" and (event.get("request") or {}).get("subtype") == "can_use_tool":
        request = event["request"]
        return [env("approval_request", request_id=event.get("request_id"),
                    tool=request.get("tool_name"), input=request.get("input") or {},
                    tool_use_id=request.get("tool_use_id"))]
    if kind == "rate_limit_event":
        return [env("rate_limit", **summarize_rate_limit(event.get("rate_limit_info")))]
    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            return [env("session_started", model=event.get("model"),
                        permission_mode=event.get("permissionMode"),
                        api_key_source_present=event.get("apiKeySource") not in (None, "none"))]
        if subtype in ("model_refusal_fallback", "api_retry"):
            return [env("notice", **summarize_notice(event))]
        return []
    if kind == "result":
        # Exit code and `subtype` are not the verdict: `is_error` is.
        return [env("turn_finished", **summarize_result(event))]
    return []


def require_session_account(account: dict[str, Any]) -> None:
    """The chat process's own initialize reply must also be a first-party subscription."""
    if account["api_provider"] != "firstParty":
        raise RuntimeRefused("not_first_party")
    if account["plan"] not in SUBSCRIPTION_PLANS:
        raise RuntimeRefused("not_a_subscription_plan")
    if account["api_key_source_present"]:
        raise RuntimeRefused("api_key_source_present")


class ClaudeSession:
    """One chat: a long-lived `claude --print` stream-json process.

    Every file edit and shell command reaches the host through `ask` rules. In
    `ask_every_edit` (the default) each one is a blocking approval card. In
    `auto_accept_trusted`, file edits whose target resolves inside a folder the
    user trusted for automatic edits are accepted by the host with the
    validated path pinned; everything else still asks.
    """

    def __init__(self, preflight: Preflight, workspace: Path, *, model: str,
                 session_id: str | None = None, resume: str | None = None,
                 trust: TrustStore, credit_consent: CreditConsent | None = None,
                 effort: str | None = None, approval_mode: str = ASK_EVERY_EDIT,
                 auto_trust: TrustedFolderStore | None = None,
                 spawn: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen) -> None:
        if not trust.is_trusted(workspace):
            raise RuntimeRefused("workspace_not_trusted")
        self.workspace = workspace.resolve()
        self.menu = list(preflight.models)
        self._credit_consent: CreditConsent | None = None
        if credit_consent is not None:
            self.grant_credit_consent(credit_consent)
        if credit_consent is not None and credit_consent.model not in (None, model):
            raise RuntimeRefused("credit_consent_for_another_model")
        for handle in (session_id, resume):
            if handle is not None and not (isinstance(handle, str) and SESSION_ID.fullmatch(handle)):
                raise RuntimeRefused("invalid_session_id")
        self._check_model(model)
        self._check_effort(model, effort)
        self.approval_mode = ASK_EVERY_EDIT
        self._turn_failed = False
        self._needs_restart = False
        self.auto_trust = auto_trust
        self._turn_active = False
        self.set_approval_mode(approval_mode, auto_trust)
        self.model = model
        # A new chat always gets a native ID up front, so the timeline and the
        # resume handle are known before the first event.
        self.session_ref = resume or session_id or str(uuid.uuid4())
        self._settings = self._write_ask_rules()
        command = [
            *preflight.executable, "--print",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--permission-prompt-tool", "stdio", "--permission-mode", "default",
            "--settings", str(self._settings),
        ]
        # Never omit --model: without it the CLI reads the model from user or project
        # settings, which the consent check cannot see. "default" is pinned to what
        # the menu says it resolves to right now.
        command += ["--model", self._spawn_model(model)]
        if effort:
            command += ["--effort", effort]
        command += ["--resume", resume] if resume else ["--session-id", self.session_ref]
        if _is_cmd_shim(preflight.executable) and any(CMD_UNSAFE.search(arg) for arg in command[1:]):
            self._settings.unlink(missing_ok=True)
            raise RuntimeRefused("unsafe_cmd_argument")
        self._backlog: collections.deque[dict[str, Any]] = collections.deque()
        self._injected: collections.deque[Envelope] = collections.deque()
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, dict[str, Any]] = {}  # tool_use_id -> {"tool", "asked"}
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._write_lock = threading.Lock()
        try:
            self.process = spawn(command, cwd=self.workspace, env=session_env(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 encoding="utf-8", errors="replace")
        except OSError:
            self._settings.unlink(missing_ok=True)
            raise RuntimeRefused("cli_spawn_failed") from None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.account: dict[str, Any] = {}
        try:
            self._initialize()
            # The live menu can differ from preflight's (it changes server-side):
            # re-check, so a default that now resolves to Fable still needs consent.
            self._check_model(model)
        except RuntimeRefused:
            self.close()
            raise

    # -- setup ---------------------------------------------------------------
    @staticmethod
    def _write_ask_rules() -> Path:
        handle, name = tempfile.mkstemp(prefix="claude-desktop-ask-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"permissions": {"ask": list(CLAUDE_ASK_RULES)}}, stream)
        return Path(name)

    def _initialize(self) -> None:
        """Handshake like the Agent SDK, and re-check billing inside this very process."""
        self._control("init", {"subtype": "initialize"})
        deadline = time.monotonic() + INITIALIZE_TIMEOUT_SECONDS
        while True:
            try:
                raw = self._events.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeRefused("initialize_timeout") from None
            if raw is None:
                self._backlog.append({"type": "__eof__"})
                raise RuntimeRefused("cli_exited_during_initialize")
            response = raw.get("response") if raw.get("type") == "control_response" else None
            if isinstance(response, dict) and response.get("request_id") == "init":
                body = response.get("response") if isinstance(response.get("response"), dict) else {}
                self.account = summarize_account(body.get("account"))
                require_session_account(self.account)
                live = local_menu(body.get("models"))
                if live:
                    self.menu = live
                return
            self._backlog.append(raw)

    def _check_model(self, model: str) -> None:
        if not isinstance(model, str) or not MODEL_VALUE.fullmatch(model):
            raise RuntimeRefused("invalid_model_value")
        folded = model.casefold()  # aliases may resolve case-insensitively in the CLI
        rows = [row for row in self.menu
                if str(row.get("value")).casefold() == folded or str(row.get("resolved_model")).casefold() == folded]
        billed = is_credit_billed(model) or any(
            row.get("credit_billed") or is_credit_billed(row.get("resolved_model"), row.get("display_name"))
            for row in rows)
        if billed and self._credit_consent is None:
            raise RuntimeRefused("model_may_bill_usage_credits")

    def _spawn_model(self, model: str) -> str:
        if model.casefold() != "default":
            return model
        return next((row["resolved_model"] for row in self.menu
                     if row.get("value") == "default" and row.get("resolved_model")), model)

    def _check_effort(self, model: str, effort: str | None) -> None:
        if effort is None:
            return
        offered = next((row["effort_levels"] for row in self.menu if row.get("value") == model), None)
        if effort not in ("low", "medium", "high", "xhigh", "max") or (offered is not None and effort not in offered):
            raise RuntimeRefused("effort_not_offered_for_model")

    def grant_credit_consent(self, consent: CreditConsent) -> None:
        """Attach the user's per-session confirmation for credit-billed models."""
        if not isinstance(consent, CreditConsent):
            raise RuntimeRefused("credit_consent_required")
        consent.bind(self)
        self._credit_consent = consent

    @property
    def credit_models_allowed(self) -> bool:
        return self._credit_consent is not None

    def set_approval_mode(self, mode: str, auto_trust: TrustedFolderStore | None = None) -> None:
        if self._turn_active:
            raise RuntimeRefused("approval_mode_change_during_turn")
        if mode not in APPROVAL_MODES:
            raise RuntimeRefused("unsupported_approval_mode")
        store = auto_trust or self.auto_trust
        if mode == AUTO_ACCEPT_TRUSTED and (store is None or not store.is_trusted(self.workspace)):
            raise RuntimeRefused("folder_not_trusted_for_auto_edits")
        self.auto_trust = store
        self.approval_mode = mode

    # -- transport -----------------------------------------------------------
    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._events.put(event)
        except (OSError, ValueError):
            pass
        self._events.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        # The UI thread's Stop button and the turn worker can both write.
        with self._write_lock:
            try:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise RuntimeRefused("cli_connection_closed") from None

    def _control(self, request_id: str, request: dict[str, Any]) -> None:
        self._send({"type": "control_request", "request_id": request_id, "request": request})

    def _next_raw(self) -> dict[str, Any] | None:
        if self._backlog:
            raw = self._backlog.popleft()
            return None if raw.get("type") == "__eof__" else raw
        return self._events.get(timeout=EVENT_TIMEOUT_SECONDS)

    # -- turns ---------------------------------------------------------------
    def send(self, text: str) -> None:
        if self._needs_restart:
            raise RuntimeRefused("session_needs_restart")
        self._send({"type": "user", "message": {"role": "user", "content": text}})
        self._turn_active = True
        self._turn_failed = False

    @property
    def needs_restart(self) -> bool:
        return self._needs_restart

    def fail_turn(self) -> None:
        """The host has failed this turn: interrupt it and decline anything it still asks for."""
        self._turn_failed = True
        try:
            self.interrupt()
        except RuntimeRefused:
            pass

    def events(self) -> Iterator[Envelope]:
        """Yield envelopes until the current turn finishes or the process exits."""
        while True:
            while self._injected:
                yield self._injected.popleft()
            try:
                raw = self._next_raw()
            except queue.Empty:
                # The CLI may still be mid-turn: stop it, and never let a later turn
                # read this turn's leftovers. The host must open a new (resumed) session.
                self.fail_turn()
                self._needs_restart = True
                self._turn_active = False
                yield Envelope(PROVIDER, self.session_ref, "turn_finished",
                               {"ok": False, "subtype": "host_timeout"})
                return
            if raw is None:
                self._turn_active = False
                yield Envelope(PROVIDER, self.session_ref, "process_exited",
                               {"exit_code": self.process.poll()})
                return
            if isinstance(raw.get("session_id"), str) and SESSION_ID.fullmatch(raw["session_id"]):
                self.session_ref = raw["session_id"]
            for envelope in normalize(raw, self.session_ref):
                yield from self._route(envelope)
                if envelope.kind == "turn_finished":
                    self._turn_active = False
                    while self._injected:
                        yield self._injected.popleft()
                    return

    def _route(self, envelope: Envelope) -> Iterator[Envelope]:
        kind, data = envelope.kind, envelope.data
        if kind == "tool_started" and isinstance(data.get("tool_use_id"), str):
            self._tools[data["tool_use_id"]] = {"tool": data.get("tool"), "asked": False}
        elif kind == "session_started" and data.get("api_key_source_present"):
            # Never bill an API key: stop the turn the moment the CLI reports one.
            self.fail_turn()
            yield envelope
            yield Envelope(PROVIDER, self.session_ref, "runtime_error", {"category": "billing",
                                                                         "code": "api_key_source_present"})
            return
        elif kind == "session_started" and is_credit_billed(data.get("model")) and self._credit_consent is None:
            # The CLI is running a credit-billed model nobody consented to (e.g. set by a settings file).
            # Refuse further turns here too, or every Send would start a Fable request again.
            self.fail_turn()
            self._needs_restart = True
            yield envelope
            yield Envelope(PROVIDER, self.session_ref, "runtime_error", {"category": "billing",
                                                                         "code": "unconsented_credit_model"})
            return
        elif kind == "approval_request":
            yield from self._approval(envelope)
            return
        elif kind == "tool_finished":
            record = self._tools.pop(str(data.get("tool_use_id")), None)
            yield envelope
            if record and record["tool"] in HOST_DECIDED_TOOLS and not record["asked"] \
                    and not data.get("is_error"):
                # An ask rule should have routed this to the host. Show it, never hide it.
                yield Envelope(PROVIDER, self.session_ref, "notice",
                               {"subtype": "ran_without_host_approval", "tool": record["tool"]})
            return
        yield envelope

    def _mark_asked(self, tool: Any, tool_use_id: Any) -> None:
        if isinstance(tool_use_id, str) and tool_use_id in self._tools:
            self._tools[tool_use_id]["asked"] = True
            return
        # Older CLIs omit tool_use_id: the request follows its own tool_use block.
        for record in reversed(list(self._tools.values())):
            if record["tool"] == tool and not record["asked"]:
                record["asked"] = True
                return

    def _approval(self, envelope: Envelope) -> Iterator[Envelope]:
        data = dict(envelope.data)
        request_id = str(data["request_id"])
        self._mark_asked(data.get("tool"), data.get("tool_use_id"))
        if self._turn_failed:
            self._respond(request_id, {"behavior": "deny", "message": "The desktop host stopped this turn"})
            yield Envelope(PROVIDER, self.session_ref, "approval_request", {**data, "policy": "declined_failed_turn"})
            yield Envelope(PROVIDER, self.session_ref, "approval_decision",
                           {"request_id": request_id, "decision": "decline", "by": "host_failed_turn"})
            return
        pinned = None
        if self.approval_mode == AUTO_ACCEPT_TRUSTED and self.auto_trust is not None \
                and self.auto_trust.is_trusted(self.workspace):
            pinned = claude_auto_accept(data.get("tool"), data.get("input"), self.workspace)
        if pinned is not None:
            self._respond(request_id, {"behavior": "allow", "updatedInput": pinned})
            yield Envelope(PROVIDER, self.session_ref, "approval_request",
                           {**data, "input": pinned, "policy": "auto_accept"})
            yield Envelope(PROVIDER, self.session_ref, "approval_decision",
                           {"request_id": request_id, "decision": "accept", "by": "auto_trusted"})
            return
        self._pending_approvals[request_id] = data["input"]
        yield Envelope(PROVIDER, self.session_ref, "approval_request", {**data, "policy": "ask"})

    def _respond(self, request_id: str, answer: dict[str, Any]) -> None:
        self._send({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": answer}})

    @property
    def pending_approvals(self) -> list[str]:
        return list(self._pending_approvals)

    def answer_approval(self, request_id: str, allow: bool, *,
                        tool_input: dict[str, Any] | None = None, message: str = "Denied") -> None:
        if request_id not in self._pending_approvals:
            raise RuntimeRefused("unknown_approval_request")
        proposed = self._pending_approvals.pop(request_id)
        # Allow runs exactly what the user saw unless the UI passes an edited input.
        answer = ({"behavior": "allow", "updatedInput": proposed if tool_input is None else tool_input}
                  if allow is True
                  else {"behavior": "deny", "message": message})
        self._respond(request_id, answer)
        self._injected.append(Envelope(PROVIDER, self.session_ref, "approval_decision", {
            "request_id": request_id, "decision": "accept" if allow is True else "decline", "by": "user"}))

    def set_model(self, model: str) -> None:
        if self._turn_active:
            raise RuntimeRefused("model_change_during_turn")
        self._check_model(model)
        target = self._spawn_model(model)  # pin whatever the menu says default means right now
        self._check_model(target)
        self._control("set_model", {"subtype": "set_model", "model": target})
        self.model = model

    def interrupt(self) -> None:
        self._control("interrupt", {"subtype": "interrupt"})

    def close(self) -> int | None:
        try:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            return self.process.wait(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            _kill_tree(self.process)
            return None
        finally:
            self._reader.join(timeout=5)
            if self.process.stdout and not self._reader.is_alive():
                self.process.stdout.close()
            self._settings.unlink(missing_ok=True)


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
    # A CLI that never answers initialize must not hang the app.
    watchdog = threading.Timer(PREFLIGHT_TIMEOUT_SECONDS, _kill_tree, args=(probe,))
    watchdog.start()
    try:
        init = initialize(_LineSession(probe))
    finally:
        watchdog.cancel()
        if probe.stdin:
            try:
                probe.stdin.close()
            except OSError:
                pass
        try:
            probe.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_tree(probe)
        if probe.stdout:
            probe.stdout.close()
    account = summarize_account(init.get("account"))
    try:
        require_subscription(auth, account)
    except ProbeRefused as refusal:
        raise RuntimeRefused(str(refusal)) from None
    return Preflight(exe, version, status, auth, account, local_menu(init.get("models")),
                     summarize_models(init.get("models")))


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
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
        except (OSError, ValueError):
            return


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
    "ASK_EVERY_EDIT", "AUTO_ACCEPT_TRUSTED", "ClaudeSession", "CreditConsent", "Envelope", "Preflight",
    "RuntimeRefused", "SUBSCRIPTION_PLANS", "TrustStore", "TrustedFolderStore", "build_handoff",
    "discover_claude", "is_credit_billed", "local_menu", "normalize", "parse_version", "preflight",
    "session_env", "version_status",
]
