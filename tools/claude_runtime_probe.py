#!/usr/bin/env python3
"""Probe the installed Claude Code CLI's headless runtime surface (VLI-157).

Drives the user's own, unmodified, already-authenticated `claude` binary the
same way the Agent SDK does (`--print` with stream-json in and out) and
reports what a desktop host can rely on: auth method, plan type, the model
menu the CLI offers this account, streaming, session resume, tool approvals,
interrupt, mid-session model switching, rate-limit events and failure shapes.

It never reads credential files, never prints account e-mail, organization or
IDs, and never uses an API key. By default it makes no model call; pass
--turns to run the probes that spend a few small turns of the user's usage.
Every probe runs in a throwaway directory with only the Write tool enabled.
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


AUTH_KEYS = ("loggedIn", "authMethod", "apiProvider")
ACCOUNT_KEYS = ("subscriptionType", "apiProvider")
MODEL_KEYS = ("value", "resolvedModel", "displayName", "supportedEffortLevels")
RATE_LIMIT_KEYS = ("status", "rateLimitType", "overageStatus", "isUsingOverage")
EVENT_TIMEOUT_SECONDS = 180


def pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """Whitelist keys so account identifiers can never leak into a report."""
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in keys if key in source}


def summarize_models(models: Any) -> list[dict[str, Any]]:
    return [pick(model, MODEL_KEYS) for model in models or [] if isinstance(model, dict)]


def summarize_rate_limit(info: Any) -> dict[str, Any]:
    summary = pick(info, RATE_LIMIT_KEYS)
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    if isinstance(windows, dict):
        summary["windows"] = {
            name: pick(window, ("utilization", "resetsAt")) for name, window in windows.items()
        }
    return summary


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    """`subtype` can read "success" on a failed turn; `is_error` is the truth."""
    return {
        "ok": not result.get("is_error", True),
        "subtype": result.get("subtype"),
        "terminal_reason": result.get("terminal_reason"),
        "api_error_status": result.get("api_error_status"),
        "permission_denials": len(result.get("permission_denials") or []),
        "models_served": sorted((result.get("modelUsage") or {}).keys()),
        "result_text": str(result.get("result") or "")[:160],
    }


def resolve_claude(command: str) -> str:
    # On Windows `claude` may be claude.exe (native install) or a claude.cmd npm shim.
    resolved = shutil.which(command)
    if not resolved:
        raise SystemExit(f"Command not found on PATH: {command}")
    return resolved


class StreamSession:
    """One `claude --print` process speaking stream-json on stdin and stdout."""

    def __init__(self, executable: str, cwd: Path, extra: list[str]) -> None:
        command = [
            executable,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            *extra,
        ]
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.events.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def control(self, request_id: str, request: dict[str, Any]) -> None:
        self.send({"type": "control_request", "request_id": request_id, "request": request})

    def user(self, text: str) -> None:
        self.send({"type": "user", "message": {"role": "user", "content": text}})

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while True:
            event = self.events.get(timeout=EVENT_TIMEOUT_SECONDS)
            if event is None:
                return
            yield event

    def close(self) -> int | None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            return self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return None


def initialize(session: StreamSession) -> dict[str, Any]:
    session.control("init", {"subtype": "initialize"})
    for event in session:
        if event.get("type") == "control_response":
            return event.get("response", {}).get("response") or {}
    return {}


def run_turn(session: StreamSession, text: str, *, decision: str | None = None,
             interrupt_after: int | None = None) -> dict[str, Any]:
    """Send one user turn and collect what a desktop UI would render from it."""
    session.user(text)
    seen: dict[str, Any] = {"text_deltas": 0, "approval_requests": [], "rate_limit": None,
                            "notices": []}
    for event in session:
        kind = event.get("type")
        if kind == "stream_event" and event["event"].get("type") == "content_block_delta":
            if event["event"].get("delta", {}).get("type") == "text_delta":
                seen["text_deltas"] += 1
                if interrupt_after and seen["text_deltas"] == interrupt_after:
                    session.control("interrupt", {"subtype": "interrupt"})
        elif kind == "control_request" and event["request"].get("subtype") == "can_use_tool":
            request = event["request"]
            seen["approval_requests"].append(request.get("tool_name"))
            if decision == "allow":
                answer = {"behavior": "allow", "updatedInput": request.get("input", {})}
            else:
                answer = {"behavior": "deny", "message": "Denied by the desktop host probe"}
            session.send({"type": "control_response", "response": {
                "subtype": "success", "request_id": event["request_id"], "response": answer}})
        elif kind == "rate_limit_event":
            seen["rate_limit"] = summarize_rate_limit(event.get("rate_limit_info"))
        elif kind == "system" and event.get("subtype") in (
                "model_refusal_fallback", "informational", "api_retry"):
            seen["notices"].append(pick(event, ("subtype", "original_model", "fallback_model",
                                                "trigger", "level")))
        elif kind == "result":
            seen.update(summarize_result(event))
            seen["session_id_returned"] = bool(event.get("session_id"))
            return seen
    seen["ok"] = False
    seen["error"] = "stream ended without a result"
    return seen


def probe(executable: str, model: str, turns: bool) -> dict[str, Any]:
    report: dict[str, Any] = {}
    version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
    report["version"] = version.stdout.strip()
    auth = subprocess.run([executable, "auth", "status", "--json"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        report["auth"] = pick(json.loads(auth.stdout), AUTH_KEYS)
    except json.JSONDecodeError:
        report["auth"] = {"parse_error": True}
    report["auth"]["exit_code"] = auth.returncode

    with tempfile.TemporaryDirectory(prefix="claude-probe-") as scratch:
        cwd = Path(scratch)
        no_tools = ["--tools", "", "--no-session-persistence", "--model", model]

        session = StreamSession(executable, cwd, no_tools)
        init = initialize(session)
        report["account"] = pick(init.get("account"), ACCOUNT_KEYS)
        report["models"] = summarize_models(init.get("models"))
        report["permission_mode"] = init.get("current_permission_mode")
        report["commands"] = len(init.get("commands") or [])
        if turns:
            report["stream_turn"] = run_turn(session, "Reply with the single word OK.")
            session.control("setmodel", {"subtype": "set_model", "model": "default"})
            report["set_model_turn"] = run_turn(session, "Reply with the single word OK.")
        session.close()
        if not turns:
            return report

        session_id = str(uuid4())
        first = StreamSession(executable, cwd, ["--tools", "", "--model", model,
                                                "--session-id", session_id])
        run_turn(first, "Remember the codeword TANGERINE and reply OK.")
        first.close()
        resumed = StreamSession(executable, cwd, ["--tools", "", "--model", model,
                                                  "--resume", session_id])
        recall = run_turn(resumed, "Which codeword did I ask you to remember? One word.")
        resumed.close()
        recall["recalled"] = "TANGERINE" in recall.get("result_text", "").upper()
        report["resume"] = recall

        approvals = ["--tools", "Write", "--permission-mode", "manual",
                     "--permission-prompt-tool", "stdio", "--no-session-persistence",
                     "--model", model]
        for decision in ("allow", "deny"):
            name = f"{decision}.txt"
            session = StreamSession(executable, cwd, approvals)
            initialize(session)
            outcome = run_turn(session, f"Use the Write tool once to create {name} "
                               "containing hi, then stop.", decision=decision)
            session.close()
            outcome["file_written"] = (cwd / name).exists()
            report[f"approval_{decision}"] = outcome

        session = StreamSession(executable, cwd, no_tools)
        report["interrupt"] = run_turn(session, "Count from 1 to 400, one number per line.",
                                       interrupt_after=5)
        session.close()

        bad = subprocess.run([executable, "--print", "--output-format", "json", "--tools", "",
                              "--no-session-persistence", "--model", "not-a-real-model"],
                             input="Reply OK", capture_output=True, text=True,
                             encoding="utf-8", errors="replace", cwd=cwd, timeout=120)
        try:
            report["unknown_model"] = {"exit_code": bad.returncode,
                                       **summarize_result(json.loads(bad.stdout))}
        except json.JSONDecodeError:
            report["unknown_model"] = {"exit_code": bad.returncode, "parse_error": True}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--model", default="haiku",
                        help="model for the probe turns (default: haiku, the cheapest)")
    parser.add_argument("--turns", action="store_true",
                        help="also run probes that make small model calls on your usage")
    args = parser.parse_args(argv)
    report = probe(resolve_claude(args.claude_command), args.model, args.turns)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
