"""Sanitized read-only Windows probe; never prints credentials or account IDs.

Run from PowerShell: python -m tools.codex_probe
Add --turn to make two tiny subscription-backed, read-only turns across a restart.
"""

import argparse
from pathlib import Path
import subprocess
import tempfile

from tools.codex_app_server import AppServerError, AppServerTransport, CodexRuntime, ThreadStore


def probe(with_turn: bool = False) -> int:
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10, check=True)
    auth = subprocess.run(["codex", "login", "status"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=15, check=False)
    print("CLI:", version.stdout.strip())
    print("CLI auth status:", "ready" if auth.returncode == 0 else "not ready")
    if auth.returncode:
        return 2
    with tempfile.TemporaryDirectory(prefix="codex-probe-") as directory:
        root = Path(directory)
        schema = subprocess.run(["codex", "app-server", "generate-json-schema", "--out", str(root / "schema")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        print("Version-matched schema generation:", "passed" if schema.returncode == 0 else "failed")
        if schema.returncode:
            return 2
        store = ThreadStore(root / "state.json")
        transport = AppServerTransport()
        try:
            runtime = CodexRuntime(transport, store)
            runtime.initialize()
            catalog = runtime.discover()
            print("Plan category:", catalog["planCategory"] or "unreported")
            print("Visible models (catalog only; turn acceptance untested):")
            for model in catalog["models"]:
                efforts = ",".join(e["reasoningEffort"] for e in model.get("supportedReasoningEfforts", []))
                print(" -", model["id"], "efforts:", efforts or "unreported")
            print("Rate-limit data:", "present" if catalog["rateLimits"] else "unreported")
            if not with_turn:
                return 0
            if not catalog["models"]:
                print("No picker-visible model to test")
                return 2
            selected = next((m for m in catalog["models"] if m.get("isDefault")), catalog["models"][0])["id"]
            workspace = root / "disposable-workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("Disposable Codex probe.\n", encoding="utf-8")
            runtime.open_thread(workspace)
            print("Selected read-only model:", selected)
            status = run_turn(runtime, selected, "Read README.md and reply with one sentence. Do not run commands or edit files.")
            print("First turn:", status)
            if status != "completed":
                return 2
        finally:
            transport.close()
        second = AppServerTransport()
        try:
            runtime = CodexRuntime(second, store)
            runtime.initialize()
            runtime.discover()
            runtime.open_thread(workspace)
            print("Native thread resume:", "accepted")
            status = run_turn(runtime, selected, "Confirm the prior request concerned README.md in one sentence. Do not run commands or edit files.")
            print("Resumed turn:", status)
            return 0 if status == "completed" else 2
        finally:
            second.close()


def run_turn(runtime: CodexRuntime, model: str, prompt: str) -> str:
    runtime.start_turn(prompt, model)
    while True:
        event = runtime.next_event(timeout=120)
        if event["kind"] == "approval_required":
            # The probe never approves execution. The first slice only supports
            # command/file decisions; interrupt for any other request type.
            try:
                runtime.decide_approval(event["requestId"], "decline")
            except ValueError:
                runtime.interrupt()
            print("Approval encountered: declined/interrupted")
        elif event["kind"] == "turn_completed":
            if event["status"] == "failed":
                print("Failure category:", event["failureKind"] or "other")
            return event["status"] or "unknown"
        elif event["kind"] == "runtime_error" and event["nativeMethod"] == "host/disconnected":
            return "disconnected"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turn", action="store_true", help="Make two tiny, read-only Codex turns and test resume")
    args = parser.parse_args()
    try:
        raise SystemExit(probe(args.turn))
    except (AppServerError, OSError, subprocess.SubprocessError, TimeoutError) as exc:
        # Errors may include service details. Do not print the raw exception.
        print("Probe failed; inspect Codex locally. No account or credential detail was printed.")
        raise SystemExit(2) from exc
