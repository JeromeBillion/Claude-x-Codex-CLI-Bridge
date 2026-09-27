#!/usr/bin/env python3
"""Probe the installed Claude Code CLI's headless runtime surface (VLI-157).

Drives the user's own, unmodified, already-authenticated `claude` binary the
same way the Agent SDK does (`--print` with stream-json in and out) and
reports what a desktop host can rely on: auth method, plan type, the model
menu the CLI offers this account, streaming, session resume, tool approvals,
interrupt, mid-session model switching, rate-limit events and failure shapes.

Safety rules the probe enforces rather than assumes:
- Child processes get an allowlisted environment only (OS, profile, locale,
  proxy/CA and Claude config location); every API key, gateway, OAuth token,
  provider switch and AWS / Google Cloud / Azure credential is dropped. Model
  turns run only after the CLI itself reports a first-party claude.ai
  subscription login with no API key source; otherwise the probe stops before
  any model call, so it can never bill an API key or a cloud account.
- A tool approval is granted only for a Write whose real target stays inside
  the throwaway probe directory; everything else is denied.
- Every string in the report is a member of a finite public set (or a fixed
  placeholder); the rest are booleans, bounded numbers and counts. Model
  output, account e-mail, organization, IDs, paths and error text never
  reach it, including on failure.
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


# ---------------------------------------------------------------------------
# Child environment: a true allowlist.
#
# The probe hands its CLI children only the variables below: OS, profile and
# locale plumbing, network proxy and CA settings, and the Claude config
# location. Every other variable is dropped: API keys, gateway URLs, OAuth
# tokens, provider switches, AWS / Google Cloud / Azure credentials, CI
# tokens, NODE_OPTIONS and anything not yet invented. So no credential the
# probe does not know about can reach the CLI.
# ---------------------------------------------------------------------------
ENV_ALLOWLIST = frozenset({
    # Windows process basics and profile locations (the CLI reads its login from the profile).
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "PUBLIC",
    "TEMP", "TMP",
    # POSIX equivalents.
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    # Locale and console encoding.
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "PYTHONIOENCODING",
    # Network reachability only; these route traffic, they do not pick a biller.
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    # Where the user's own Claude Code config and login live, and its Windows shell.
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH",
})
# Values the probe sets itself, overriding anything inherited.
ENV_FORCED = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",  # connectors are not what the probe measures
    "DISABLE_AUTOUPDATER": "1",  # the probe must not change the CLI it is measuring
}
# Documented categories the allowlist removes (kept for tests and the reply;
# the allowlist, not this list, is what enforces the rule).
STRIPPED_CATEGORIES = {
    "anthropic_api": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                      "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    "claude_code_tokens_and_switches": ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                                        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_CODE_USE_BEDROCK",
                                        "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                                        "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
                                        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"),
    "aws": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
            "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK", "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN"),
    "google_cloud": ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT",
                     "CLOUDSDK_CONFIG", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", "CLOUD_ML_REGION",
                     "ANTHROPIC_VERTEX_PROJECT_ID", "VERTEX_REGION_CLAUDE_FABLE_5_1"),
    "azure_foundry": ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
                      "AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_FEDERATED_TOKEN_FILE",
                      "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE", "IDENTITY_ENDPOINT",
                      "IDENTITY_HEADER", "MSI_ENDPOINT", "MSI_SECRET"),
    "other_llm_and_ci_tokens": ("OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN"),
    "code_injection": ("NODE_OPTIONS", "NODE_PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"),
}


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child environment from the allowlist only (case-insensitive, for Windows)."""
    source = dict(os.environ if base is None else base)
    env = {name: value for name, value in source.items() if name.upper() in ENV_ALLOWLIST}
    env.update(ENV_FORCED)
    return env


# ---------------------------------------------------------------------------
# Output: every reported string is a member of a finite public set.
#
# A CLI-origin value is printed only if it is exactly one of the public
# values below. Anything else becomes a fixed placeholder, or for model IDs a
# family bucket such as "unlisted-claude-opus" that keeps no part of the
# original string. No regex shape check, no hashing and no truncation: a
# short private value that merely looks like an enum is never echoed.
# ---------------------------------------------------------------------------
UNLISTED = "unlisted"
AUTH_METHODS = frozenset({"claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party", "none"})
API_PROVIDERS = frozenset({"firstParty", "bedrock", "vertex", "foundry"})
# Plan labels the CLI reports. Only the first four mean usage comes out of a Claude plan.
SUBSCRIPTION_PLANS = frozenset({"Claude Pro", "Claude Max", "Claude Team", "Claude Enterprise"})
PLANS = SUBSCRIPTION_PLANS | {"Claude API"}
PERMISSION_MODES = frozenset({"default", "manual", "acceptEdits", "plan", "auto", "dontAsk",
                              "bypassPermissions"})
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
MODEL_ALIASES = frozenset({"default", "best", "fable", "opus", "sonnet", "haiku", "opusplan"})
MODEL_IDS = frozenset({
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5",
    "claude-haiku-4-5", "claude-haiku-4-5-20251001",
})
MODEL_FAMILIES = ("fable", "opus", "sonnet", "haiku")
DISPLAY_NAMES = frozenset({"Default (recommended)", "Default", "Best", "Fable", "Opus", "Sonnet",
                           "Haiku", "Opus Plan", "Opus Plan Mode"})
RATE_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
RATE_LIMIT_TYPES = frozenset({"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
                              "seven_day_fable", "overage"})
NOTICE_SUBTYPES = frozenset({"model_refusal_fallback", "api_retry"})
NOTICE_TRIGGERS = frozenset({"refusal", "rate_limited", "overloaded", "unavailable", "server_denied"})
RESULT_SUBTYPES = frozenset({"success", "error_max_turns", "error_during_execution",
                             "error_max_budget_usd", "error_max_structured_output_retries"})
TERMINAL_REASONS = frozenset({"completed", "aborted_streaming", "aborted_tools", "api_error",
                              "max_turns", "blocking_limit", "turn_setup_failed",
                              "structured_output_retry_exhausted", "tool_deferred_unavailable"})
# The only models the probe itself will spend turns on: the cheap plan models.
PROBE_MODELS = frozenset({"haiku", "sonnet", "claude-haiku-4-5", "claude-sonnet-5"})
VERSION = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,5})")
EVENT_TIMEOUT_SECONDS = 180
RECALL_WORD = "TANGERINE"


class ProbeRefused(RuntimeError):
    """The probe stopped before any model call to protect billing or privacy."""


def enum(value: Any, allowed: frozenset[str]) -> str | None:
    """Echo a value only if it is exactly a known public enum member."""
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else UNLISTED


def model_name(value: Any) -> str | None:
    """Known public model IDs and aliases pass; anything else keeps only its family."""
    if value is None:
        return None
    if not isinstance(value, str):
        return UNLISTED
    base, suffix = (value[:-4], "[1m]") if value.endswith("[1m]") else (value, "")
    if base in MODEL_IDS or base in MODEL_ALIASES:
        return base + suffix
    for family in MODEL_FAMILIES:
        if base.startswith(f"claude-{family}-"):
            return f"unlisted-claude-{family}"
    return UNLISTED


def utilization(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
        return None
    return round(float(value), 2)


def epoch_seconds(value: Any) -> int | None:
    """Reset times are public clock values; anything outside 2020–2096 is dropped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if 1_577_836_800 <= value <= 4_000_000_000 else None


def http_status(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


def count(value: Any) -> int:
    return min(len(value), 10_000) if isinstance(value, (list, dict)) else 0


def version_string(text: Any) -> str | None:
    match = VERSION.search(text) if isinstance(text, str) else None
    return ".".join(str(int(part)) for part in match.groups()) if match else None


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
    # `auth status` also returns email, orgId, orgName and config paths; none are read.
    raw = raw if isinstance(raw, dict) else {}
    return {
        "logged_in": raw.get("loggedIn") is True,
        "auth_method": enum(raw.get("authMethod"), AUTH_METHODS),
        "api_provider": enum(raw.get("apiProvider"), API_PROVIDERS),
        "api_key_source_present": raw.get("apiKeySource") not in (None, "none"),
        "exit_code": exit_code if isinstance(exit_code, int) and -255 <= exit_code <= 255 else None,
    }


def summarize_account(account: Any) -> dict[str, Any]:
    account = account if isinstance(account, dict) else {}
    return {
        "plan": enum(account.get("subscriptionType"), PLANS),
        "api_provider": enum(account.get("apiProvider"), API_PROVIDERS),
        "api_key_source_present": account.get("apiKeySource") not in (None, "none"),
    }


def summarize_models(models: Any) -> list[dict[str, Any]]:
    summary = []
    for model in models if isinstance(models, list) else []:
        if not isinstance(model, dict):
            continue
        levels = model.get("supportedEffortLevels")
        summary.append({
            "value": model_name(model.get("value")),
            "resolved_model": model_name(model.get("resolvedModel")),
            "display_name": enum(model.get("displayName"), DISPLAY_NAMES),
            "effort_levels": sorted({level for level in levels if level in EFFORT_LEVELS})
            if isinstance(levels, list) else [],
        })
    return summary[:50]


def summarize_rate_limit(info: Any) -> dict[str, Any]:
    info = info if isinstance(info, dict) else {}
    summary: dict[str, Any] = {
        "status": enum(info.get("status"), RATE_LIMIT_STATUSES),
        "type": enum(info.get("rateLimitType"), RATE_LIMIT_TYPES),
        "overage_status": enum(info.get("overageStatus"), RATE_LIMIT_STATUSES),
        "using_overage": info.get("isUsingOverage") is True,
    }
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        # Only known window names become keys; an unknown key is never echoed.
        summary["windows"] = {
            name: {"utilization": utilization(window.get("utilization")),
                   "resets_at": epoch_seconds(window.get("resetsAt"))}
            for name, window in windows.items()
            if name in RATE_LIMIT_TYPES and isinstance(window, dict)
        }
        summary["unlisted_windows"] = sum(1 for name in windows if name not in RATE_LIMIT_TYPES)
    return summary


def summarize_notice(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "subtype": enum(event.get("subtype"), NOTICE_SUBTYPES),
        "original_model": model_name(event.get("original_model")),
        "fallback_model": model_name(event.get("fallback_model")),
        "trigger": enum(event.get("trigger"), NOTICE_TRIGGERS),
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
        "subtype": enum(result.get("subtype"), RESULT_SUBTYPES),
        "terminal_reason": enum(result.get("terminal_reason"), TERMINAL_REASONS),
        "api_error_status": http_status(result.get("api_error_status")),
        "permission_denials": count(result.get("permission_denials")),
        "models_served": sorted({model_name(name) for name in usage}) if isinstance(usage, dict) else [],
    }


def result_text(result: Any) -> str:
    text = result.get("result") if isinstance(result, dict) else None
    return text if isinstance(text, str) else ""


class CliNotFound(RuntimeError):
    """No `claude` executable on PATH."""


def resolve_claude(command: str) -> list[str]:
    # On Windows `claude` may be claude.exe (native install) or a claude.cmd npm shim.
    resolved = shutil.which(command)
    if not resolved:
        raise CliNotFound()
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
    if auth["auth_method"] != "claude.ai":
        raise ProbeRefused("not_a_claude_ai_login")
    if auth["api_provider"] != "firstParty" or account["api_provider"] != "firstParty":
        raise ProbeRefused("not_first_party")
    if account["plan"] not in SUBSCRIPTION_PLANS:
        raise ProbeRefused("not_a_subscription_plan")
    if auth["api_key_source_present"] or account["api_key_source_present"]:
        raise ProbeRefused("api_key_source_present")


def probe(executable: Sequence[str], model: str, turns: bool) -> dict[str, Any]:
    report: dict[str, Any] = {}
    version = run_cli(executable, ["--version"], timeout=30)
    report["version"] = version_string(version.stdout)
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
        report["permission_mode"] = enum(init.get("current_permission_mode"), PERMISSION_MODES)
        report["commands"] = count(init.get("commands"))
        if not turns:
            session.close()
            return report
        try:
            if model not in PROBE_MODELS:
                raise ProbeRefused("model_not_allowed_for_probe")
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
            report["unknown_model"] = {"exit_code": summarize_auth({}, bad.returncode)["exit_code"],
                                       **summarize_result(json.loads(bad.stdout))}
        except json.JSONDecodeError:
            report["unknown_model"] = {"exit_code": summarize_auth({}, bad.returncode)["exit_code"],
                                       "parse_error": True}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--model", default="haiku", choices=sorted(PROBE_MODELS),
                        help="model for the probe turns (default: haiku, the cheapest; "
                             "Fable and Opus are not offered)")
    parser.add_argument("--turns", action="store_true",
                        help="also run probes that make small model calls on your subscription")
    args = parser.parse_args(argv)
    # Errors are reported as a fixed code only: no exception text, paths or CLI output.
    try:
        report = probe(resolve_claude(args.claude_command), args.model, args.turns)
    except CliNotFound:
        report = {"error": "claude_not_found"}
    except subprocess.TimeoutExpired:
        report = {"error": "cli_timeout"}
    except OSError:
        report = {"error": "cli_spawn_failed"}
    except Exception:  # noqa: BLE001 - anything else still yields only a fixed code
        report = {"error": "internal_error"}
    print(json.dumps(report, indent=2))
    if "error" in report:
        return 3
    return 2 if "turns_refused" in report else 0


if __name__ == "__main__":
    sys.exit(main())
