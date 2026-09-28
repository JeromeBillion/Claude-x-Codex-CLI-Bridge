"""Protocol and safety tests without an authenticated Codex installation."""

import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_app_server import (AppServerError, AppServerTransport, CodexRuntime,
                                    ThreadStore, TrustedFolderStore, normalize_app_event)
from tools.runtime_events import Envelope
from claude_runtime import normalize as normalize_claude


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
    def test_full_catalog_includes_hidden_entries_and_uses_callable_model_name(self):
        class CatalogTransport(FakeTransport):
            def request(self, method, params=None):
                if method == "model/list":
                    self.calls.append((method, params))
                    return {"data": [{"id": "picker-id", "model": "callable-id", "hidden": True,
                                      "supportedReasoningEfforts": []}]}
                return super().request(method, params)

        with tempfile.TemporaryDirectory() as tmp:
            fake = CatalogTransport()
            runtime = CodexRuntime(fake, ThreadStore(Path(tmp) / "state.json"))
            runtime.discover()
            self.assertEqual(set(runtime.models), {"picker-id"})
            self.assertTrue(next(params for method, params in fake.calls if method == "model/list")["includeHidden"])
            runtime.open_thread(Path(tmp))
            runtime.start_turn("hello", "picker-id")
            self.assertEqual(fake.calls[-1][1]["model"], "callable-id")

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
            self.assertEqual(fake.calls[-1][1]["sandboxPolicy"], {"type": "readOnly"})
            with self.assertRaises(ValueError):
                runtime.start_turn("test", "unlisted")
            runtime.interrupt()
            self.assertEqual(fake.calls[-1][1]["turnId"], "turn_1")
            restarted = CodexRuntime(FakeTransport(), store)
            restarted.discover()
            restarted.open_thread(workspace)
            method, params = restarted.transport.calls[-1]
            self.assertEqual(method, "thread/resume")
            self.assertEqual(params["threadId"], "thr_native")
            self.assertEqual(params["sandbox"], "readOnly")

    def test_approvals_require_explicit_decision_and_other_requests_stay_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTransport()
            runtime = CodexRuntime(fake, ThreadStore(Path(tmp) / "state.json"))
            fake.events.put({"id": 8, "method": "item/commandExecution/requestApproval", "params": {"command": "git status"}})
            event = runtime.next_event()
            self.assertEqual(event.kind, "approval_request")
            self.assertEqual(event.session_ref, None)
            self.assertEqual(fake.answers, [])
            with self.assertRaises(ValueError):
                runtime.decide_approval(8, "acceptForSession")
            runtime.decide_approval(8, "decline")
            self.assertEqual(fake.answers, [(8, {"decision": "decline"})])
            self.assertEqual(runtime.next_event().kind, "approval_decision")
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
            self.assertEqual(runtime.next_event().data["text"], "Hello")
            self.assertEqual(runtime.next_event().data["terminal_reason"], "limit")

    def test_rate_account_disconnect_and_approval_decision_envelopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTransport()
            runtime = CodexRuntime(fake, ThreadStore(Path(tmp) / "state.json"))
            runtime.thread_id = "native-thread"
            fake.events.put({"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 91, "resetsAt": 123}}}})
            fake.events.put({"method": "account/updated", "params": {"authMode": None, "email": "secret@example.com"}})
            fake.events.put({"method": "host/disconnected", "params": {"exitCode": 1}})
            self.assertEqual(runtime.next_event().kind, "rate_limit")
            self.assertEqual(runtime.next_event().data, {"auth_mode": None, "plan_category": None})
            self.assertEqual(runtime.next_event().data, {"category": "auth"})
            exit_event = runtime.next_event()
            self.assertEqual(exit_event.kind, "process_exited")
            self.assertEqual(exit_event.session_ref, "native-thread")

    def test_special_approval_families_are_blocking(self):
        for method, kind in [("mcpServer/elicitation/request", "mcp_elicitation"),
                             ("tool/requestUserInput", "connector_approval_request"),
                             ("item/permissions/requestApproval", "approval_request")]:
            with self.subTest(method=method):
                event = normalize_app_event({"id": 7, "method": method,
                                             "params": {"threadId": "native"}}, None)[0]
                self.assertEqual(event.kind, kind)
                self.assertEqual(event.session_ref, "native")

    def test_approval_toggle_scopes_auto_edits_to_explicit_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            trust = TrustedFolderStore(Path(tmp) / "trusted.json")
            runtime = CodexRuntime(FakeTransport(), ThreadStore(Path(tmp) / "state.json"))
            runtime.discover()
            with self.assertRaises(AppServerError):
                runtime.open_thread(workspace, approval_mode="auto_accept_trusted", trusted_folders=trust)
            runtime.open_thread(workspace)
            self.assertEqual(runtime.transport.calls[-1][1]["sandbox"], "readOnly")
            trust.trust(workspace)  # represents the user's explicit click
            runtime.set_approval_mode("auto_accept_trusted", workspace, trust)
            runtime.start_turn("Work here", "first")
            policy = runtime.transport.calls[-1][1]["sandboxPolicy"]
            self.assertEqual(policy["type"], "workspaceWrite")
            self.assertEqual(policy["writableRoots"], [str(workspace.resolve())])
            self.assertFalse(policy["networkAccess"])
            with self.assertRaises(AppServerError):
                runtime.set_approval_mode("ask_every_edit", workspace, trust)

    def test_shared_envelope_kind_conformance(self):
        equivalents = [
            ({"method": "item/agentMessage/delta", "params": {"delta": "hi"}},
             {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}),
            ({"method": "item/reasoning/summaryTextDelta", "params": {"delta": "private"}},
             {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "private"}}}),
            ({"method": "item/started", "params": {"item": {"type": "commandExecution", "id": "one"}}},
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "one"}]}}),
            ({"method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "one", "status": "completed"}}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "one"}]}}),
            ({"id": 4, "method": "item/commandExecution/requestApproval", "params": {"threadId": "codex-native"}},
             {"type": "control_request", "request_id": "four", "request": {"subtype": "can_use_tool", "tool_name": "Bash"}}),
            ({"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
             {"type": "result", "is_error": False, "subtype": "success"}),
        ]
        for codex, claude in equivalents:
            with self.subTest(codex=codex["method"]):
                ce = normalize_app_event(codex, "codex-native")[0]
                cl = normalize_claude(claude, "claude-native")[0]
                self.assertIsInstance(ce, Envelope)
                self.assertIsInstance(cl, Envelope)
                self.assertEqual(ce.kind, cl.kind)
                self.assertEqual(ce.session_ref, "codex-native")
                self.assertEqual(cl.session_ref, "claude-native")
        self.assertEqual(normalize_app_event({"method": "turn/completed", "params": {"turn": {"status": "failed"}}}, "c")[0].data["ok"], False)
        self.assertEqual(normalize_app_event({"method": "account/updated", "params": {"authMode": "chatgpt", "email": "private@example.com"}}, "c")[0].data,
                         {"auth_mode": "chatgpt", "plan_category": None})

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
