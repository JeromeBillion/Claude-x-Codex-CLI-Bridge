#!/usr/bin/env python3
"""Probe the installed Claude Code CLI's headless runtime surface (VLI-157).

Drives the user's own, unmodified, already-authenticated `claude` binary the
same way the Agent SDK does (`--print` with stream-json in and out) and
reports what a desktop host can rely on: auth method, plan type, the model
menu the CLI offers this account, streaming, session resume, tool approvals,
interrupt, mid-session model switching, rate-limit events and failure shapes.

Safety rules the probe enforces rather than assumes:
- API-key and alternate-provider variables are removed from every child
  environment, and model turns run only after the CLI itself reports a
  first-party Claude subscription login. Otherwise the probe stops before any
  model call, so it can never bill an API key.
- A tool approval is granted only for a Write whose real target stays inside
  the throwaway probe directory; everything else is denied.
- The report holds only allowlisted, pattern-checked fields, booleans and
  counts. Model output, account e-mail, organization and IDs never reach it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path, PurePath
from typing import Any, Iterator, Sequence
from uuid import uuid4


# Environment variables that would make the CLI bill something other than the
# signed-in subscription (API keys, gateways, or third-party providers).
BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_BEARER_TOKEN_BEDROCK",
)
# Plan labels the CLI reports for subscription logins; anything else ("Claude
# API" included) means usage would not come out of a Claude plan.
SUBSCRIPTION_PLANS = frozenset({"Claude Pro", "Claude Max", "Claude Team", "Claude Enterprise"})
# Fable turns can bill usage credits without a consent prompt in -p mode.
CREDIT_BILLED_MODELS = re.compile(r"fable|best", re.IGNORECASE)
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,62}(\[1m\])?$")
LABEL = re.compile(r"^[A-Za-z0-9 ().\-]{1,40}$")
VERSION = re.compile(r"\d+\.\d+\.\d+")
EVENT_TIMEOUT_SECONDS = 180
RECALL_WORD = "TANGERINE"


class ProbeRefused(RuntimeError):
    """The probe stopped before any model call to protect billing or privacy."""


def safe(value: Any, pattern: re.Pattern[str]) -> Any:
    """Keep a CLI-reported string only if it matches the expected shape."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return "<unrecognized>"


def safe_number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy the environment without anything that could route billing elsewhere."""
    env = dict(os.environ if base is None else base)
    blocked = {name.upper() for name in BILLING_ENV_VARS}
    for name in list(env):
        if name.upper() in blocked:
            del env[name]
    # Keep claude.ai connectors out of the probe; they are not what it measures.
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    return env


def confined_write_target(tool_input: Any, root: Path) -> Path | None:
    """Return the resolved Write target if it stays inside root, else None."""
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    if any(part == ".." for part in PurePath(raw.replace("\\", "/")).parts):
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        real_root = root.resolve(strict=True)
        # resolve() follows every symlink or junction that already exists.
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved == real_root or not resolved.is_relative_to(real_root):
        return None
    if resolved.exists() and not resolved.is_file():
        return None
    return resolved


def summarize_auth(raw: Any, exit_code: int) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "logged_in": raw.get("loggedIn") is True,
        "auth_method": safe(raw.get("authMethod"), TOKEN),
        "api_provider": safe(raw.get("apiProvider"), TOKEN),
        "exit_code": exit_code,
    }


def summarize_account(account: Any) -> dict[str, Any]:
    account = account if isinstance(account, dict) else {}
    plan = account.get("subscriptionType")
    return {
        "plan": plan if plan in SUBSCRIPTION_PLANS else safe(plan, LABEL),
        "api_provider": safe(account.get("apiProvider"), TOKEN),
        "api_key_source_present": account.get("apiKeySource") not in (None, "none"),
    }


def summarize_models(models: Any) -> list[dict[str, Any]]:
    summary = []
    for model in models if isinstance(models, list) else []:
        if not isinstance(model, dict):
            continue
        levels = model.get("supportedEffortLevels")
        summary.append({
            "value": safe(model.get("value"), MODEL_ID),
            "resolved_model": safe(model.get("resolvedModel"), MODEL_ID),
            "display_name": safe(model.get("displayName"), LABEL),
            "effort_levels": [level for level in levels if level in EFFORT_LEVELS]
            if isinstance(levels, list) else [],
        })
    return summary


def summarize_rate_limit(info: Any) -> dict[str, Any]:
    info = info if isinstance(info, dict) else {}
    summary = {
        "status": safe(info.get("status"), TOKEN),
        "type": safe(info.get("rateLimitType"), TOKEN),
        "overage_status": safe(info.get("overageStatus"), TOKEN),
        "using_overage": info.get("isUsingOverage") is True,
    }
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        summary["windows"] = {
            safe(name, TOKEN): {
                "utilization": safe_number(window.get("utilization")),
                "resets_at": safe_number(window.get("resetsAt")),
            }
            for name, window in windows.items() if isinstance(window, dict)
        }
    return summary


def summarize_notice(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "subtype": safe(event.get("subtype"), TOKEN),
        "original_model": safe(event.get("original_model"), MODEL_ID),
        "fallback_model": safe(event.get("fallback_model"), MODEL_ID),
        "trigger": safe(event.get("trigger"), TOKEN),
    }


def summarize_result(result: Any) -> dict[str, Any]:
    """`subtype` can read "success" on a failed turn; `is_error` is the truth.

    The model's reply text is deliberately left out: it is untrusted and can
    quote anything the model saw. Checks that need it run inside the probe.
    """
    result = result if isinstance(result, dict) else {}
    usage = result.get("modelUsage")
    return {
        "ok": result.get("is_error") is False,
        "subtype": safe(result.get("subtype"), TOKEN),
        "terminal_reason": safe(result.get("terminal_reason"), TOKEN),
        "api_error_status": safe_number(result.get("api_error_status")),
        "permission_denials": len(result.get("permission_denials") or []),
        "models_served": sorted(safe(name, MODEL_ID) for name in usage)
        if isinstance(usage, dict) else [],
    }


def result_text(result: Any) -> str:
    text = result.get("result") if isinstance(result, dict) else None
    return text if isinstance(text, str) else ""


def resolve_claude(command: str) -> list[str]:
    # On Windows `claude` may be claude.exe (native install) or a claude.cmd npm shim.
    resolved = shutil.which(command)
    if not resolved:
        raise SystemExit(f"Command not found on PATH: {command}")
    return [resolved]


def run_cli(executable: Sequence[str], arguments: Sequence[str], *, cwd: Path | None = None,
            stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*executable, *arguments], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", cwd=cwd,
                          env=child_env(), timeout=timeout, check=False)


class StreamSession:
    """One `claude --print` process speaking stream-json on stdin and stdout."""

    def __init__(self, executable: Sequence[str], cwd: Path, extra: Sequence[str]) -> None:
        command = [
            *executable,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            # Only this invocation's flags: no user or project settings, hooks or MCP servers.
            "--setting-sources",
            "",
            "--strict-mcp-config",
            *extra,
        ]
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self.events.put(event)
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
            try:
                event = self.events.get(timeout=EVENT_TIMEOUT_SECONDS)
            except queue.Empty:
                return
            if event is None:
                return
            yield event

    def close(self) -> int | None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            return self.process.wait(timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()
            return None
        finally:
            self.reader.join(timeout=5)
            if self.process.stdout and not self.reader.is_alive():
                self.process.stdout.close()


def initialize(session: Any) -> dict[str, Any]:
    session.control("init", {"subtype": "initialize"})
    for event in session:
        if event.get("type") == "control_response":
            body = (event.get("response") or {}).get("response")
            return body if isinstance(body, dict) else {}
    return {}


def approval_answer(request: dict[str, Any], decision: str | None,
                    write_root: Path | None) -> tuple[dict[str, Any], bool]:
    """Decide a can_use_tool request. Returns (answer, guard_rejected)."""
    deny = {"behavior": "deny", "message": "Denied by the desktop host probe"}
    if decision != "allow":
        return deny, False
    if request.get("tool_name") != "Write" or write_root is None:
        return deny, True
    target = confined_write_target(request.get("input"), write_root)
    if target is None:
        return deny, True
    # Pin the approved input to the validated absolute path.
    return {"behavior": "allow", "updatedInput": {**request["input"], "file_path": str(target)}}, False


def run_turn(session: Any, text: str, *, decision: str | None = None,
             write_root: Path | None = None,
             interrupt_after: int | None = None) -> tuple[dict[str, Any], str]:
    """Send one user turn. Returns (shareable summary, private reply text)."""
    session.user(text)
    seen: dict[str, Any] = {"text_deltas": 0, "approval_requests": 0, "approvals_granted": 0,
                            "guard_rejections": 0, "rate_limit": None, "notices": []}
    for event in session:
        kind = event.get("type")
        if kind == "stream_event" and isinstance(event.get("event"), dict):
            inner = event["event"]
            if inner.get("type") == "content_block_delta" and \
                    (inner.get("delta") or {}).get("type") == "text_delta":
                seen["text_deltas"] += 1
                if interrupt_after and seen["text_deltas"] == interrupt_after:
                    session.control("interrupt", {"subtype": "interrupt"})
        elif kind == "control_request" and (event.get("request") or {}).get("subtype") == "can_use_tool":
            seen["approval_requests"] += 1
            answer, rejected = approval_answer(event["request"], decision, write_root)
            seen["guard_rejections"] += rejected
            seen["approvals_granted"] += answer["behavior"] == "allow"
            session.send({"type": "control_response", "response": {
                "subtype": "success", "request_id": event.get("request_id"), "response": answer}})
        elif kind == "rate_limit_event":
            seen["rate_limit"] = summarize_rate_limit(event.get("rate_limit_info"))
        elif kind == "system" and event.get("subtype") in ("model_refusal_fallback", "api_retry"):
            seen["notices"].append(summarize_notice(event))
        elif kind == "result":
            seen.update(summarize_result(event))
            return seen, result_text(event)
    seen["ok"] = False
    seen["subtype"] = "no_result"
    return seen, ""


def require_subscription(auth: dict[str, Any], account: dict[str, Any]) -> None:
    """Refuse model turns unless the CLI itself reports a subscription login."""
    if not auth["logged_in"] or auth["exit_code"] != 0:
        raise ProbeRefused("not_logged_in")
    if auth["api_provider"] != "firstParty" or account["api_provider"] != "firstParty":
        raise ProbeRefused("not_first_party")
    if account["plan"] not in SUBSCRIPTION_PLANS:
        raise ProbeRefused("not_a_subscription_plan")
    if account["api_key_source_present"]:
        raise ProbeRefused("api_key_source_present")


def probe(executable: Sequence[str], model: str, turns: bool) -> dict[str, Any]:
    report: dict[str, Any] = {}
    version = run_cli(executable, ["--version"], timeout=30)
    match = VERSION.search(version.stdout)
    report["version"] = match.group(0) if match else None
    status = run_cli(executable, ["auth", "status", "--json"], timeout=30)
    try:
        raw_auth = json.loads(status.stdout)
    except json.JSONDecodeError:
        raw_auth = None
    report["auth"] = summarize_auth(raw_auth, status.returncode)

    with tempfile.TemporaryDirectory(prefix="claude-probe-") as scratch:
        cwd = Path(scratch)
        base = ["--no-session-persistence", "--model", model, "--effort", "low", "--max-turns", "2"]

        session = StreamSession(executable, cwd, ["--tools", "", *base])
        init = initialize(session)
        report["account"] = summarize_account(init.get("account"))
        report["models"] = summarize_models(init.get("models"))
        report["permission_mode"] = safe(init.get("current_permission_mode"), TOKEN)
        report["commands"] = len(init.get("commands") or [])
        if not turns:
            session.close()
            return report
        try:
            if CREDIT_BILLED_MODELS.search(model):
                raise ProbeRefused("model_may_bill_usage_credits")
            require_subscription(report["auth"], report["account"])
        except ProbeRefused as refusal:
            session.close()
            report["turns_refused"] = str(refusal)
            return report

        report["stream_turn"], _ = run_turn(session, "Reply with the single word OK.")
        session.control("setmodel", {"subtype": "set_model", "model": "sonnet"})
        report["set_model_turn"], _ = run_turn(session, "Reply with the single word OK.")
        session.close()

        session_id = str(uuid4())
        persisted = ["--tools", "", "--model", model, "--effort", "low", "--max-turns", "2"]
        first = StreamSession(executable, cwd, [*persisted, "--session-id", session_id])
        run_turn(first, f"Remember the codeword {RECALL_WORD} and reply OK.")
        first.close()
        resumed = StreamSession(executable, cwd, [*persisted, "--resume", session_id])
        recall, reply = run_turn(resumed, "Which codeword did I ask you to remember? One word.")
        resumed.close()
        recall["recalled"] = RECALL_WORD in reply.upper()
        report["resume"] = recall

        approvals = ["--tools", "Write", "--permission-mode", "manual",
                     "--permission-prompt-tool", "stdio", *base]
        for decision in ("allow", "deny"):
            name = f"{decision}.txt"
            session = StreamSession(executable, cwd, approvals)
            initialize(session)
            outcome, _ = run_turn(session, f"Use the Write tool once to create {name} "
                                  "containing hi, then stop.", decision=decision, write_root=cwd)
            session.close()
            outcome["file_written"] = (cwd / name).exists()
            report[f"approval_{decision}"] = outcome

        session = StreamSession(executable, cwd, ["--tools", "", *base])
        report["interrupt"], _ = run_turn(session, "Count from 1 to 400, one number per line.",
                                          interrupt_after=5)
        session.close()

        bad = run_cli(executable, ["--print", "--output-format", "json", "--tools", "",
                                   "--setting-sources", "", "--strict-mcp-config",
                                   "--no-session-persistence", "--model", "not-a-real-model"],
                      cwd=cwd, stdin="Reply OK", timeout=120)
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
                        help="model for the probe turns (default: haiku, the cheapest; "
                             "Fable is refused because it can bill usage credits)")
    parser.add_argument("--turns", action="store_true",
                        help="also run probes that make small model calls on your subscription")
    args = parser.parse_args(argv)
    report = probe(resolve_claude(args.claude_command), args.model, args.turns)
    print(json.dumps(report, indent=2))
    return 2 if "turns_refused" in report else 0


if __name__ == "__main__":
    sys.exit(main())
