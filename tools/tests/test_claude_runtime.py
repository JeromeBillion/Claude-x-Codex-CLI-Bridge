from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_runtime_probe as probe_module  # noqa: E402
from claude_runtime import (  # noqa: E402
    ClaudeSession,
    Envelope,
    RuntimeRefused,
    TrustStore,
    build_handoff,
    discover_claude,
    normalize,
    session_env,
    parse_version,
    preflight,
    version_status,
)

# Variables that pick or pay Claude's biller: removed from every child, sessions included.
SESSION_PLANTED = {
    "ANTHROPIC_API_KEY": "PLANTED-1", "anthropic_auth_token": "PLANTED-2", "ANTHROPIC_BASE_URL": "PLANTED-3",
    "ANTHROPIC_VERTEX_PROJECT_ID": "PLANTED-4", "CLAUDE_CODE_USE_BEDROCK": "PLANTED-5",
    "CLAUDE_CODE_USE_VERTEX": "PLANTED-6", "CLAUDE_CODE_OAUTH_TOKEN": "PLANTED-7",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "PLANTED-8", "AWS_BEARER_TOKEN_BEDROCK": "PLANTED-9",
    "CLOUD_ML_REGION": "PLANTED-10", "VERTEX_REGION_CLAUDE_OPUS_5_5": "PLANTED-11", "NODE_OPTIONS": "PLANTED-12",
}
# General cloud credentials: dropped from preflight (allowlist); kept in chat sessions for the user's own work.
PREFLIGHT_ONLY_PLANTED = {"AWS_PROFILE": "PLANTED-dev", "AWS_SECRET_ACCESS_KEY": "PLANTED-13",
                          "GOOGLE_APPLICATION_CREDENTIALS": "PLANTED-14", "AZURE_CLIENT_SECRET": "PLANTED-15"}

FAKE_CLI = [sys.executable, str(Path(__file__).resolve().parent / "fake_claude_cli.py")]


class VersionAndDiscoveryTests(unittest.TestCase):
    def test_version_guard(self) -> None:
        self.assertEqual(parse_version("2.1.283 (Claude Code)"), (2, 1, 283))
        self.assertEqual(version_status((2, 1, 283)), "tested")
        self.assertEqual(version_status((2, 2, 0)), "newer_untested")
        self.assertEqual(version_status((2, 1, 100)), "too_old")
        self.assertEqual(version_status(parse_version("garbage")), "unknown")

    def test_discovery_falls_back_to_the_native_windows_install(self) -> None:
        with tempfile.TemporaryDirectory() as profile:
            exe = Path(profile) / ".local" / "bin" / "claude.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(discover_claude({"PATH": "", "USERPROFILE": profile}), [str(exe)])
            exe.unlink()
            with self.assertRaises(RuntimeRefused):
                discover_claude({"PATH": "", "USERPROFILE": profile})


class ImportTests(unittest.TestCase):
    def test_adapter_imports_from_any_directory_and_as_a_package(self) -> None:
        repo = TOOLS_DIR.parent
        checks = {
            "flat, outside the repo": (f"import sys; sys.path.insert(0, {str(TOOLS_DIR)!r}); import claude_runtime",
                                       tempfile.gettempdir()),
            "package, from the repo root": ("import tools.claude_runtime", str(repo)),
            "shared envelope class": (
                "import sys; sys.path.insert(0, 'tools'); import claude_runtime, tools.runtime_events; "
                "assert claude_runtime.Envelope is tools.runtime_events.Envelope", str(repo)),
        }
        for name, (code, cwd) in checks.items():
            with self.subTest(name):
                done = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True,
                                      timeout=60, check=False)
                self.assertEqual(done.returncode, 0, done.stderr[-500:])


class NormalizeTests(unittest.TestCase):
    def kinds(self, event: dict) -> list[str]:
        return [envelope.kind for envelope in normalize(event, "s1")]

    def test_maps_every_stream_shape_the_ui_needs(self) -> None:
        text = {"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta", "text": "hi"}}}
        self.assertEqual(normalize(text, "s1")[0], Envelope("claude", "s1", "text_delta", {"text": "hi"}))
        tool_use = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x"}, {"type": "tool_use", "name": "Edit", "id": "t1"}]}}
        self.assertEqual(self.kinds(tool_use), ["tool_started"])
        tool_result = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}}
        self.assertEqual(normalize(tool_result, "s1")[0].data, {"tool_use_id": "t1", "is_error": True})
        approval = {"type": "control_request", "request_id": "r", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}}}
        self.assertEqual(self.kinds(approval), ["approval_request"])
        self.assertEqual(self.kinds({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}),
                         ["rate_limit"])
        self.assertEqual(self.kinds({"type": "system", "subtype": "model_refusal_fallback"}), ["notice"])
        self.assertEqual(self.kinds({"type": "system", "subtype": "thinking_tokens"}), [])
        self.assertEqual(self.kinds({"type": "something_new"}), [])

    def test_turn_verdict_comes_from_is_error_not_subtype(self) -> None:
        envelope = normalize({"type": "result", "subtype": "success", "is_error": True,
                              "api_error_status": 404, "result": "free text"}, "s1")[0]
        self.assertFalse(envelope.data["ok"])
        self.assertEqual(envelope.data["api_error_status"], 404)
        self.assertNotIn("result", envelope.data)


class TrustTests(unittest.TestCase):
    def test_trust_is_explicit_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = TrustStore(Path(root) / "state" / "trust.json")
            workspace = Path(root)
            self.assertFalse(store.is_trusted(workspace))
            store.trust(workspace)
            self.assertTrue(TrustStore(store.path).is_trusted(workspace))


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log = self.root / "calls.jsonl"
        self.env = patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(self.log), "FAKE_PLAN": "Claude Max",
                                           **SESSION_PLANTED, **PREFLIGHT_ONLY_PLANTED})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Test plumbing for the fake CLI is the only addition to the preflight allowlist.
        self.allow = patch.object(probe_module, "ENV_ALLOWLIST",
                                  probe_module.ENV_ALLOWLIST | {"FAKE_CLAUDE_LOG", "FAKE_PLAN"})
        self.allow.start()
        self.addCleanup(self.allow.stop)
        self.trust = TrustStore(self.root / "trust.json")
        self.trust.trust(self.root)
        self.pre = preflight(FAKE_CLI)

    def open(self, **kwargs) -> ClaudeSession:
        session = ClaudeSession(self.pre, self.root, model=kwargs.pop("model", "sonnet"),
                                trust=self.trust, **kwargs)
        self.addCleanup(session.close)
        return session

    def test_preflight_reports_plan_and_models_without_a_model_call(self) -> None:
        self.assertEqual(self.pre.account["plan"], "Claude Max")
        self.assertEqual(self.pre.version_status, "tested")
        self.assertEqual(self.pre.models[0]["value"], "haiku")
        self.assertNotIn("user_turn", self.log.read_text(encoding="utf-8"))

    def test_preflight_refuses_an_api_billed_login(self) -> None:
        with patch.dict(os.environ, {"FAKE_PLAN": "Claude API"}), self.assertRaises(RuntimeRefused) as caught:
            preflight(FAKE_CLI)
        self.assertEqual(str(caught.exception), "not_a_subscription_plan")

    def test_preflight_times_out_instead_of_hanging(self) -> None:
        script = self.root / "silent_cli.py"
        script.write_text(
            "import json, sys, time\n"
            "if '--version' in sys.argv: print('2.1.283')\n"
            "elif 'auth' in sys.argv: print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'}))\n"
            "else: time.sleep(30)\n", encoding="utf-8")
        started = time.monotonic()
        with patch("claude_runtime.PREFLIGHT_TIMEOUT_SECONDS", 1), self.assertRaises(RuntimeRefused) as caught:
            preflight([sys.executable, str(script)])
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(str(caught.exception), "not_first_party")

    def test_untrusted_workspace_and_silent_credit_models_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as other, self.assertRaises(RuntimeRefused) as caught:
            ClaudeSession(self.pre, Path(other), model="sonnet", trust=self.trust)
        self.assertEqual(str(caught.exception), "workspace_not_trusted")
        with self.assertRaises(RuntimeRefused):
            ClaudeSession(self.pre, self.root, model="fable", trust=self.trust)
        session = self.open()
        with self.assertRaises(RuntimeRefused):
            session.set_model("claude-fable-5-1")

    def test_turn_streams_text_rate_limit_notice_and_verdict(self) -> None:
        session = self.open(session_id="11111111-2222-3333-4444-555555555555")
        session.send("Reply OK")
        kinds = [envelope.kind for envelope in session.events()]
        self.assertEqual(kinds, ["text_delta", "rate_limit", "notice", "turn_finished"])

    def test_approval_round_trip_allows_exactly_the_proposed_input(self) -> None:
        session = self.open()
        session.send("Use the Write tool once to create note.txt containing hi.")
        events = session.events()
        request = next(events)
        self.assertEqual(request.kind, "approval_request")
        with self.assertRaises(RuntimeRefused):
            session.answer_approval("forged-id", True)
        session.answer_approval(request.data["request_id"], True)
        self.assertEqual([e.kind for e in events], ["turn_finished"])
        self.assertTrue((self.root / "note.txt").exists())

    def test_interrupt_ends_the_turn_as_an_error(self) -> None:
        session = self.open()
        session.send("Count from 1 to 400.")
        finished = None
        for count, envelope in enumerate(session.events()):
            if count == 2:
                session.interrupt()
            if envelope.kind == "turn_finished":
                finished = envelope
        self.assertIsNotNone(finished)
        self.assertEqual((finished.data["ok"], finished.data["terminal_reason"]), (False, "aborted_streaming"))

    def test_resume_and_set_model_use_the_native_flags(self) -> None:
        session = self.open(resume="11111111-2222-3333-4444-555555555555")
        session.set_model("opus")
        session.send("Reply OK")
        list(session.events())
        argv = [line for line in self.log.read_text(encoding="utf-8").splitlines() if "--resume" in line]
        self.assertTrue(argv)

    def test_no_billing_variable_reaches_any_child(self) -> None:
        session = self.open()
        session.send("Reply OK")
        list(session.events())
        session.close()
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        spawned = [call for call in calls if "argv" in call]
        preflight_calls = [c for c in spawned if "--tools" in c["argv"] or c["argv"][:1] in (["--version"], ["auth"])]
        session_calls = [c for c in spawned if "--permission-prompt-tool" in c["argv"]]
        self.assertTrue(preflight_calls and session_calls)
        for call in spawned:
            names = {name.upper() for name in call["env_names"]}
            self.assertEqual(names & {name.upper() for name in SESSION_PLANTED}, set(), call["argv"])
        for call in preflight_calls:
            self.assertEqual(call["env_values_with_planted"], [], call["argv"])
        # Chat sessions keep the user's own dev tooling credentials; they cannot pick Claude's biller.
        self.assertIn("AWS_PROFILE", session_calls[0]["env_names"])

    def test_session_env_blocks_every_billing_route(self) -> None:
        env = session_env({**SESSION_PLANTED, "PATH": "/bin", "JAVA_HOME": "/j", "AWS_PROFILE": "dev"})
        self.assertEqual(set(env) - {"PATH", "JAVA_HOME", "AWS_PROFILE", "DISABLE_AUTOUPDATER"}, set())


class HandoffTests(unittest.TestCase):
    def test_handoff_is_explicit_bounded_and_disclaims_native_transfer(self) -> None:
        envelopes = [Envelope("claude", "s1", "tool_started", {"tool": "Edit"}),
                     Envelope("claude", "s1", "text_delta", {"text": "Fixed the ledger bug. " * 1000}),
                     Envelope("claude", "s1", "turn_finished", {"ok": True})]
        packet = build_handoff(Path("/work/ledger"), envelopes, user_summary="Continue with tests.",
                               files=["src/ledger.py"], limit_chars=2_000)
        self.assertLessEqual(len(packet), 2_000)
        self.assertIn("not resumable in Codex", packet)
        self.assertIn("Tools used: Edit", packet)
        self.assertIn("src/ledger.py", packet)
        self.assertIn("Continue with tests.", packet)


if __name__ == "__main__":
    unittest.main()
