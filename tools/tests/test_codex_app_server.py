"""Protocol and safety tests without an authenticated Codex installation."""

import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_app_server import AppServerError, AppServerTransport, CodexRuntime, ThreadStore


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.events = queue.Queue()
        self.answers = []

    def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "initialize":
            return {}
        if method == "account/read":
            return {"account": {"type": "chatgpt", "planType": "plus"}}
        if method == "model/list":
            if params.get("cursor"):
                return {"data": [{"id": "second", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
            return {"data": [{"id": "first", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}],
                    "nextCursor": "page-2"}
        if method == "account/rateLimits/read":
            return {"rateLimits": {"primary": {"usedPercent": 25}}}
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thr_native"}}
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        return {}

    def notify(self, method, params=None):
        self.calls.append((method, params))

    def answer(self, request_id, result):
        self.answers.append((request_id, result))


class RuntimeTests(unittest.TestCase):
    def test_catalog_turn_and_resume_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = ThreadStore(workspace / "local-state.json")
            fake = FakeTransport()
            runtime = CodexRuntime(fake, store)
            runtime.initialize()
            discovered = runtime.discover()
            self.assertEqual(discovered["planCategory"], "plus")
            self.assertEqual(set(runtime.models), {"first", "second"})
            self.assertEqual(runtime.open_thread(workspace), "thr_native")
            self.assertEqual(fake.calls[-1][1]["sandbox"], "readOnly")
            runtime.start_turn("Read this repository", "second", "high")
            with self.assertRaises(ValueError):
                runtime.start_turn("test", "unlisted")
            runtime.interrupt()
            self.assertEqual(fake.calls[-1][1]["turnId"], "turn_1")
            restarted = CodexRuntime(FakeTransport(), store)
            restarted.discover()
            restarted.open_thread(workspace)
            self.assertEqual(restarted.transport.calls[-1], ("thread/resume", {"threadId": "thr_native"}))

    def test_approvals_require_explicit_decision_and_other_requests_stay_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTransport()
            runtime = CodexRuntime(fake, ThreadStore(Path(tmp) / "state.json"))
            fake.events.put({"id": 8, "method": "item/commandExecution/requestApproval", "params": {"command": "git status"}})
            event = runtime.next_event()
            self.assertEqual(event["kind"], "approval_required")
            self.assertEqual(fake.answers, [])
            with self.assertRaises(ValueError):
                runtime.decide_approval(8, "acceptForSession")
            runtime.decide_approval(8, "decline")
            self.assertEqual(fake.answers, [(8, {"decision": "decline"})])
            fake.events.put({"id": 9, "method": "item/permissions/requestApproval", "params": {"reason": "network"}})
            runtime.next_event()
            with self.assertRaises(ValueError):
                runtime.decide_approval(9, "accept")

    def test_stream_and_failure_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTransport()
            runtime = CodexRuntime(fake, ThreadStore(Path(tmp) / "state.json"))
            fake.events.put({"method": "item/agentMessage/delta", "params": {"threadId": "thr_native", "turnId": "t", "delta": "Hello"}})
            fake.events.put({"method": "turn/completed", "params": {"turn": {"id": "t", "status": "failed", "error": {"codexErrorInfo": {"type": "UsageLimitExceeded"}}}}})
            self.assertEqual(runtime.next_event()["text"], "Hello")
            self.assertEqual(runtime.next_event()["failureKind"], "limit")

    def test_api_key_account_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTransport()
            original = fake.request
            fake.request = lambda method, params=None: {"account": {"type": "apiKey"}} if method == "account/read" else original(method, params)
            with self.assertRaises(AppServerError):
                CodexRuntime(fake, ThreadStore(Path(tmp) / "state.json")).discover()


class TransportTests(unittest.TestCase):
    def test_stdio_framing_notifications_and_shell_denial(self):
        script = '''import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        print(json.dumps({"id": m["id"], "result": {"platformOs": "fake"}}), flush=True)
    elif m.get("method") == "initialized":
        print(json.dumps({"method": "item/agentMessage/delta", "params": {"delta": "streamed"}}), flush=True)
    elif m.get("method") == "account/read":
        print(json.dumps({"id": m["id"], "result": {"account": {"type": "chatgpt"}}}), flush=True)
'''
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "fake.py"
            fake_path.write_text(script, encoding="utf-8")
            real_popen = subprocess.Popen

            def spawn(*args, **kwargs):
                return real_popen([sys.executable, "-u", str(fake_path)], **kwargs)

            with patch("tools.codex_app_server.subprocess.Popen", side_effect=spawn):
                transport = AppServerTransport(timeout=2)
                try:
                    self.assertEqual(transport.request("initialize")["platformOs"], "fake")
                    transport.notify("initialized")
                    self.assertEqual(transport.events.get(timeout=2)["params"]["delta"], "streamed")
                    self.assertEqual(transport.request("account/read")["account"]["type"], "chatgpt")
                    with self.assertRaises(ValueError):
                        transport.request("thread/shellCommand", {"command": "echo unsafe"})
                finally:
                    transport.close()


if __name__ == "__main__":
    unittest.main()
