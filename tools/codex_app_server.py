"""Local Codex App Server adapter for a single-user desktop host.

The host owns UI and persistence location. Codex owns authentication and execution.
No API-key login, credential inspection, or out-of-sandbox shell command is exposed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any

from tools.runtime_events import Envelope
# Shared with the Claude adapter: one explicit list of folders trusted for automatic edits.
from tools.approval_modes import TrustedFolderStore  # noqa: F401 - re-exported


class AppServerError(RuntimeError):
    pass


class AppServerTransport:
    """Newline-delimited JSON-RPC over an installed CLI's stdio pipes."""

    ALLOWED = frozenset({
        "initialize", "account/read", "account/rateLimits/read", "model/list",
        "thread/start", "thread/resume", "thread/read", "thread/list",
        "turn/start", "turn/steer", "turn/interrupt", "skills/list",
        "mcpServerStatus/list", "app/installed",
    })

    def __init__(self, executable: str = "codex", *, timeout: float = 15.0,
                 env: dict[str, str] | None = None) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, shell=False, env=env,
        )
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 0
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read, name="codex-app-server-reader", daemon=True).start()

    def _send(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as exc:
                raise AppServerError("Codex App Server connection closed") from exc

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.events.put({"method": "host/protocolError", "params": {"message": "Invalid JSON from App Server"}})
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and "method" not in message:
                with self._pending_lock:
                    waiter = self._pending.pop(message["id"], None)
                if waiter is not None:
                    waiter.put(message)
            else:
                self.events.put(message)
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter.put({"error": {"message": "Codex App Server exited"}})
        self.events.put({"method": "host/disconnected", "params": {"exitCode": self.process.poll()}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method not in self.ALLOWED:
            raise ValueError(f"Unsupported App Server request: {method}")
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = waiter
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
            try:
                response = waiter.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise AppServerError(f"Timed out waiting for {method}") from exc
            if "error" in response:
                error = response["error"]
                raise AppServerError(f"{method}: {error.get('message', 'unknown error')}")
            return response.get("result", {})
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method != "initialized":
            raise ValueError(f"Unsupported notification: {method}")
        self._send({"method": method, "params": params or {}})

    def answer(self, request_id: int | str, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "result": result})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


class ThreadStore:
    """Persist only native thread identifiers; caller selects a private local path."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, workspace: Path) -> str | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return data.get(str(workspace.resolve()))

    def put(self, workspace: Path, thread_id: str) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        data[str(workspace.resolve())] = thread_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, self.path)


def _failure_kind(error: dict[str, Any] | None) -> str | None:
    if not isinstance(error, dict):
        return None
    info = error.get("codexErrorInfo") or {}
    name = info if isinstance(info, str) else info.get("type", "")
    status = info.get("httpStatusCode") if isinstance(info, dict) else None
    if name == "UsageLimitExceeded":
        return "limit"
    if name == "Unauthorized" or status in {401, 403}:
        return "auth"
    if name in {"HttpConnectionFailed", "ResponseStreamConnectionFailed", "ResponseStreamDisconnected"}:
        return "connection"
    return "other"


def normalize_app_event(message: dict[str, Any], session_ref: str | None) -> list[Envelope]:
    """Map App Server notifications/requests to Claude's shared UI kinds.

    Private error text and account identifiers stay on the transport, not in
    the envelope. The UI may display local command/file details for approval.
    """
    method = message.get("method", "")
    p = message.get("params") or {}
    ref = p.get("threadId") or session_ref

    def env(kind: str, **data: Any) -> list[Envelope]:
        return [Envelope("codex", ref, kind, data)]

    if "id" in message and method:
        request_id = message["id"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            family = "command" if "commandExecution" in method else "file_change"
            return env("approval_request", request_id=request_id, family=family,
                       tool=family, item_id=p.get("itemId"), turn_id=p.get("turnId"),
                       command=p.get("command") if family == "command" else None,
                       cwd=p.get("cwd"), reason=p.get("reason"),
                       available_decisions=p.get("availableDecisions"))
        if method == "mcpServer/elicitation/request":
            return env("mcp_elicitation", request_id=request_id, server=p.get("serverName"),
                       mode=p.get("mode"), turn_id=p.get("turnId"))
        if method in {"tool/requestUserInput", "item/tool/requestUserInput"}:
            return env("connector_approval_request", request_id=request_id,
                       turn_id=p.get("turnId"), family="tool_input")
        return env("approval_request", request_id=request_id, family="permissions_or_unknown",
                   turn_id=p.get("turnId"))
    if method == "item/agentMessage/delta":
        return env("text_delta", text=p.get("delta", ""))
    if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta", "item/reasoning/summaryPartAdded"}:
        return env("thinking_delta")
    if method in {"item/started", "item/completed"}:
        item = p.get("item") or {}
        typ = item.get("type")
        if typ in {"commandExecution", "fileChange", "mcpToolCall", "webSearch", "collabToolCall"}:
            return env("tool_started" if method == "item/started" else "tool_finished",
                       tool=typ, tool_use_id=item.get("id"),
                       is_error=item.get("status") in {"failed", "declined"} if method == "item/completed" else False)
        return []
    if method == "thread/started":
        thread = p.get("thread") or {}
        return [Envelope("codex", thread.get("id") or ref, "session_started", {})]
    if method == "turn/completed":
        turn = p.get("turn") or {}
        error = turn.get("error") or {}
        info = error.get("codexErrorInfo") or {}
        return env("turn_finished", ok=turn.get("status") == "completed",
                   subtype=turn.get("status"), terminal_reason=_failure_kind(error),
                   api_error_status=info.get("httpStatusCode") if isinstance(info, dict) else None)
    if method == "account/rateLimits/updated":
        limits = p.get("rateLimits") or {}
        primary = limits.get("primary") or {}
        return env("rate_limit", status="rejected" if limits.get("rateLimitReachedType") else "allowed",
                   type=limits.get("limitId"), used_percent=primary.get("usedPercent"),
                   resets_at=primary.get("resetsAt"))
    if method == "account/updated":
        status = env("account_status", auth_mode=p.get("authMode"), plan_category=p.get("planType"))
        if p.get("authMode") != "chatgpt":
            status += env("runtime_error", category="auth")
        return status
    if method == "error":
        return env("runtime_error", category=_failure_kind(p.get("error")))
    if method in {"host/disconnected", "host/protocolError"}:
        return env("process_exited" if method == "host/disconnected" else "runtime_error",
                   exit_code=p.get("exitCode") if method == "host/disconnected" else None,
                   category="connection")
    if method in {"warning", "configWarning", "model/rerouted", "turn/plan/updated"}:
        return env("notice", subtype=method)
    return []


class CodexRuntime:
    """A minimal UI-facing slice. The host must service approvals while a turn runs."""

    def __init__(self, transport: AppServerTransport, store: ThreadStore) -> None:
        self.transport = transport
        self.store = store
        self.thread_id: str | None = None
        self.models: dict[str, dict[str, Any]] = {}
        self.account: dict[str, Any] | None = None
        self.active_turn_id: str | None = None
        self._approvals: dict[int | str, str] = {}
        self._ui_events: queue.Queue[Envelope] = queue.Queue()
        self.workspace: Path | None = None
        self.approval_mode = "ask_every_edit"

    def initialize(self) -> dict[str, Any]:
        result = self.transport.request("initialize", {"clientInfo": {
            "name": "claude_codex_personal_bridge", "title": "Claude x Codex Desktop", "version": "0.1.0",
        }})
        self.transport.notify("initialized")
        return result

    def discover(self) -> dict[str, Any]:
        account = self.transport.request("account/read", {"refreshToken": False}).get("account")
        self.account = account
        if not account or account.get("type") != "chatgpt":
            raise AppServerError("Sign in to Codex with ChatGPT before starting a subscription turn")
        self.models.clear()
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            # The desktop must expose the installed CLI's full account catalog.
            # Hidden entries are labeled by the UI, not silently discarded.
            params: dict[str, Any] = {"limit": 100, "includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            page = self.transport.request("model/list", params)
            for model in page.get("data", []):
                if isinstance(model.get("id"), str):
                    self.models[model["id"]] = model
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise AppServerError("Repeated model catalog cursor")
            seen.add(cursor)
        return {"planCategory": account.get("planType"), "models": list(self.models.values()),
                "rateLimits": self.transport.request("account/rateLimits/read")}

    def open_thread(self, workspace: Path, *, approval_mode: str = "ask_every_edit",
                    trusted_folders: TrustedFolderStore | None = None) -> str:
        if not self.account or self.account.get("type") != "chatgpt":
            raise AppServerError("ChatGPT account has not been verified")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Workspace must be a directory")
        self.set_approval_mode(approval_mode, workspace, trusted_folders)
        stored = self.store.get(workspace)
        if stored:
            result = self.transport.request("thread/resume", {
                "threadId": stored, "cwd": str(workspace),
                "sandbox": self._thread_sandbox_name(), "approvalPolicy": "on-request",
            })
        else:
            result = self.transport.request("thread/start", {
                "cwd": str(workspace), "sandbox": self._thread_sandbox_name(),
                "approvalPolicy": "on-request",
            })
        self.thread_id = result["thread"]["id"]
        self.store.put(workspace, self.thread_id)
        return self.thread_id

    def set_approval_mode(self, mode: str, workspace: Path,
                          trusted_folders: TrustedFolderStore | None) -> None:
        if self.active_turn_id:
            raise AppServerError("Cannot change approval mode during an active turn")
        if mode not in {"ask_every_edit", "auto_accept_trusted"}:
            raise ValueError("Unsupported approval mode")
        workspace = workspace.resolve(strict=True)
        if mode == "auto_accept_trusted" and (trusted_folders is None or not trusted_folders.is_trusted(workspace)):
            raise AppServerError("Explicitly trust this folder before enabling automatic edits")
        self.workspace = workspace
        self.approval_mode = mode

    def _sandbox_name(self) -> str:
        return "workspaceWrite" if self.approval_mode == "auto_accept_trusted" else "readOnly"

    def _thread_sandbox_name(self) -> str:
        # Thread start/resume use SandboxMode's kebab-case enum. Turn/start
        # uses SandboxPolicy.type's distinct camel-case enum.
        return "workspace-write" if self.approval_mode == "auto_accept_trusted" else "read-only"

    def start_turn(self, text: str, model: str, effort: str | None = None) -> dict[str, Any]:
        if not self.thread_id:
            raise AppServerError("Open a thread first")
        if model not in self.models:
            raise ValueError("Model is absent from the account's visible model catalog")
        supported = {entry["reasoningEffort"] for entry in self.models[model].get("supportedReasoningEfforts", [])}
        if effort and effort not in supported:
            raise ValueError("Reasoning effort is absent from this model's catalog entry")
        assert self.workspace is not None
        callable_model = self.models[model].get("model") or model
        if not isinstance(callable_model, str):
            raise ValueError("Catalog entry has no callable model name")
        params: dict[str, Any] = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}], "model": callable_model,
                                  "approvalPolicy": "on-request", "cwd": str(self.workspace),
                                  "sandboxPolicy": {"type": self._sandbox_name()}}
        if self.approval_mode == "auto_accept_trusted":
            params["sandboxPolicy"].update({"writableRoots": [str(self.workspace)], "networkAccess": False})
        if effort:
            params["effort"] = effort
        result = self.transport.request("turn/start", params)
        self.active_turn_id = result["turn"]["id"]
        return result

    def steer(self, turn_id: str, text: str) -> dict[str, Any]:
        return self.transport.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": turn_id,
                                                     "input": [{"type": "text", "text": text}]})

    def interrupt(self) -> dict[str, Any]:
        if not self.active_turn_id:
            raise AppServerError("No active turn to interrupt")
        return self.transport.request("turn/interrupt", {"threadId": self.thread_id,
                                                         "turnId": self.active_turn_id})

    def next_event(self, timeout: float | None = None) -> Envelope:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                return self._ui_events.get_nowait()
            except queue.Empty:
                pass
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            message = self.transport.events.get(timeout=remaining)
            event = self._process_message(message)
            if event is not None:
                return event

    def _process_message(self, message: dict[str, Any]) -> Envelope | None:
        method = message.get("method", "")
        params = message.get("params", {})
        if "id" in message and "method" in message:
            self._approvals[message["id"]] = method
        if method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("id") == self.active_turn_id:
                self.active_turn_id = None
        if method == "account/updated" and params.get("authMode") != "chatgpt":
            self.account = None
        if method == "serverRequest/resolved":
            self._approvals.pop(params.get("requestId"), None)
        envelopes = normalize_app_event(message, self.thread_id)
        for extra in envelopes[1:]:
            self._ui_events.put(extra)
        return envelopes[0] if envelopes else None

    def decide_approval(self, request_id: int | str, decision: str) -> None:
        method = self._approvals.get(request_id)
        if method not in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            raise ValueError("Approval type requires a dedicated UI handler")
        if decision not in {"accept", "decline", "cancel"}:
            raise ValueError("Unsupported approval decision")
        self.transport.answer(request_id, {"decision": decision})
        del self._approvals[request_id]
        self._ui_events.put(Envelope("codex", self.thread_id, "approval_decision",
                                     {"request_id": request_id, "decision": decision}))
