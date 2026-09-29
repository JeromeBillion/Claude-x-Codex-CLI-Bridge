"""Sanitized probe for Jerome's own signed-in Windows Codex CLI.

PowerShell from the repository root: py -3 -m tools.codex_probe
Add --turn only to authorize two tiny subscription-backed read-only turns.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
from typing import Any

from tools.codex_app_server import AppServerTransport, CodexRuntime, ThreadStore


# Reviewed official CLI release/schema target; fake-server tests exercise our
# protocol implementation. A real Windows run remains an explicit open gate.
TESTED_VERSION = "0.158.0-alpha.2.1"
VERSION_RE = re.compile(r"^codex-cli (\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*$")
# Public IDs reviewed for this probe target. Catalog entries outside this set
# are counted, never echoed: a shape check would disclose a short private ID.
PUBLIC_MODEL_IDS = frozenset({
    "gpt-5.6-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
})
EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max", "ultra"})
PLANS = frozenset({"free", "go", "plus", "pro", "business", "edu", "enterprise"})
ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TEMP", "TMP",
    "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CODEX_HOME",
})


def safe_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Only OS/profile/network plumbing reaches any probe child, including preflight."""
    return {name: value for name, value in (os.environ if source is None else source).items()
            if name.upper() in ENV_ALLOWLIST}


def safe_model_id(value: Any) -> str:
    return value if isinstance(value, str) and value in PUBLIC_MODEL_IDS else "unlisted"


def version_id(value: str) -> str | None:
    match = VERSION_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def run_turn(runtime: CodexRuntime, model: str, prompt: str) -> str:
    runtime.start_turn(prompt, model)
    while True:
        event = runtime.next_event(timeout=120)
        if event.kind == "approval_request":
            try:
                runtime.decide_approval(event.data["request_id"], "decline")
            except ValueError:
                runtime.interrupt()
        elif event.kind in {"mcp_elicitation", "connector_approval_request"}:
            runtime.interrupt()
        elif event.kind == "turn_finished":
            return "passed" if event.data.get("ok") is True else "failed"
        elif event.kind == "process_exited":
            return "failed"


def probe(with_turn: bool = False, *, executable: str = "codex") -> dict[str, Any]:
    """Return an allowlisted report; raw CLI responses never leave this function."""
    report: dict[str, Any] = {
        "cli_version": "unavailable", "version_check": "failed", "auth_check": "failed",
        "schema_check": "failed", "handshake_check": "failed", "subscription_check": "failed",
        "catalog_check": "failed", "plan_category": "unreported", "models": [],
        "rate_limit_check": "failed", "turn_check": "not_requested", "resume_check": "not_requested",
    }
    resolved = shutil.which(executable)
    if not resolved:
        return report
    env = safe_child_env()
    try:
        version_run = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", env=env, timeout=10, check=False)
        version = version_id(version_run.stdout) if version_run.returncode == 0 else None
        if version is None:
            return report
        report["cli_version"] = version
        report["version_check"] = "tested_target" if version == TESTED_VERSION else "different_version"
        auth = subprocess.run([resolved, "login", "status"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env, timeout=15, check=False)
        if auth.returncode:
            return report
        report["auth_check"] = "passed"
        with tempfile.TemporaryDirectory(prefix="codex-probe-") as directory:
            root = Path(directory)
            schema = subprocess.run([resolved, "app-server", "generate-json-schema", "--out", str(root / "schema")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                    timeout=30, check=False)
            if schema.returncode:
                return report
            report["schema_check"] = "passed"
            store = ThreadStore(root / "state.json")
            transport = AppServerTransport(resolved, env=env)
            try:
                runtime = CodexRuntime(transport, store)
                runtime.initialize()
                report["handshake_check"] = "passed"
                # discover() fails closed for API-key, PAT or no account. Its
                # account response is never placed in the report.
                catalog = runtime.discover()
                report["subscription_check"] = "passed"
                plan = catalog["planCategory"]
                report["plan_category"] = plan if plan in PLANS else "unreported"
                report["models"] = [{
                    "id": safe_model_id(model.get("id")),
                    "efforts": [e["reasoningEffort"] for e in model.get("supportedReasoningEfforts", [])
                                if isinstance(e, dict) and e.get("reasoningEffort") in EFFORTS],
                } for model in catalog["models"] if isinstance(model, dict)]
                report["catalog_check"] = "passed"
                report["rate_limit_check"] = "passed" if catalog["rateLimits"] else "unreported"
                if not with_turn:
                    return report
                # An explicit flag is necessary but a changed CLI still needs
                # a fresh protocol review before any model/billing call.
                if version != TESTED_VERSION or not catalog["models"]:
                    report["turn_check"] = "refused_version_or_catalog"
                    return report
                selected = next((m for m in catalog["models"] if m.get("isDefault")), catalog["models"][0])["id"]
                workspace = root / "disposable-workspace"
                workspace.mkdir()
                (workspace / "README.md").write_text("Disposable Codex probe.\n", encoding="utf-8")
                runtime.open_thread(workspace)
                report["turn_check"] = run_turn(runtime, selected, "Read README.md and reply with one sentence. Do not run commands or edit files.")
                if report["turn_check"] != "passed":
                    return report
            finally:
                transport.close()
            second = AppServerTransport(resolved, env=env)
            try:
                runtime = CodexRuntime(second, store)
                runtime.initialize()
                runtime.discover()
                runtime.open_thread(workspace)
                report["resume_check"] = run_turn(runtime, selected,
                    "Confirm the earlier request concerned README.md. Do not run commands or edit files.")
            finally:
                second.close()
    except (OSError, subprocess.SubprocessError, RuntimeError, queue.Empty, ValueError, KeyError, TypeError):
        # Never print a raw exception: service errors can echo private input.
        report["probe_error"] = "failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turn", action="store_true", help="Authorize two small read-only subscription turns")
    args = parser.parse_args()
    report = probe(args.turn)
    print(json.dumps(report, indent=2))
    required = ("version_check", "auth_check", "schema_check", "handshake_check",
                "subscription_check", "catalog_check")
    return 0 if all(report[key] in {"passed", "tested_target", "different_version"} for key in required) \
        and (not args.turn or report["turn_check"] == report["resume_check"] == "passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
