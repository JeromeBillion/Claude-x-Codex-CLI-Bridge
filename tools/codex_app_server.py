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
from typing import Any


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

    def __init__(self, executable: str = "codex", *, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, shell=False,
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
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            page = self.transport.request("model/list", params)
            for model in page.get("data", []):
                if not model.get("hidden"):
                    self.models[model["id"]] = model
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise AppServerError("Repeated model catalog cursor")
            seen.add(cursor)
        return {"planCategory": account.get("planType"), "models": list(self.models.values()),
                "rateLimits": self.transport.request("account/rateLimits/read")}

    def open_thread(self, workspace: Path, *, writable: bool = False) -> str:
        if not self.account or self.account.get("type") != "chatgpt":
            raise AppServerError("ChatGPT account has not been verified")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Workspace must be a directory")
        stored = self.store.get(workspace)
        if stored:
            result = self.transport.request("thread/resume", {"threadId": stored})
        else:
            result = self.transport.request("thread/start", {
                "cwd": str(workspace), "sandbox": "workspaceWrite" if writable else "readOnly",
                "approvalPolicy": "onRequest",
            })
        self.thread_id = result["thread"]["id"]
        self.store.put(workspace, self.thread_id)
        return self.thread_id

    def start_turn(self, text: str, model: str, effort: str | None = None) -> dict[str, Any]:
        if not self.thread_id:
            raise AppServerError("Open a thread first")
        if model not in self.models:
            raise ValueError("Model is absent from the account's visible model catalog")
        supported = {entry["reasoningEffort"] for entry in self.models[model].get("supportedReasoningEfforts", [])}
        if effort and effort not in supported:
            raise ValueError("Reasoning effort is absent from this model's catalog entry")
        params: dict[str, Any] = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}], "model": model}
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

    def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        message = self.transport.events.get(timeout=timeout)
        method = message.get("method", "")
        params = message.get("params", {})
        base = {"provider": "codex", "threadId": params.get("threadId", self.thread_id), "nativeMethod": method}
        if "id" in message and "method" in message:
            self._approvals[message["id"]] = method
            return {**base, "kind": "approval_required", "requestId": message["id"], "details": params}
        if method == "item/agentMessage/delta":
            return {**base, "kind": "text_delta", "text": params.get("delta", ""), "turnId": params.get("turnId")}
        if method in {"item/started", "item/updated", "item/completed"}:
            return {**base, "kind": "item", "phase": method.split("/")[1], "item": params.get("item"),
                    "turnId": params.get("turnId")}
        if method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("id") == self.active_turn_id:
                self.active_turn_id = None
            return {**base, "kind": "turn_completed", "turn": turn, "status": turn.get("status"),
                    "error": turn.get("error"), "failureKind": self._failure_kind(turn.get("error"))}
        if method == "error":
            return {**base, "kind": "runtime_error", "details": params,
                    "failureKind": self._failure_kind(params.get("error"))}
        if method in {"account/rateLimits/updated", "account/updated"}:
            if method == "account/updated" and params.get("authMode") != "chatgpt":
                self.account = None
            return {**base, "kind": "account_status", "details": params}
        if method in {"host/disconnected", "host/protocolError", "warning", "configWarning"}:
            return {**base, "kind": "runtime_error", "details": params}
        return {**base, "kind": "activity", "details": params}

    def decide_approval(self, request_id: int | str, decision: str) -> None:
        method = self._approvals.get(request_id)
        if method not in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            raise ValueError("Approval type requires a dedicated UI handler")
        if decision not in {"accept", "decline", "cancel"}:
            raise ValueError("Unsupported approval decision")
        self.transport.answer(request_id, {"decision": decision})
        del self._approvals[request_id]

    @staticmethod
    def _failure_kind(error: dict[str, Any] | None) -> str | None:
        if not error:
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
