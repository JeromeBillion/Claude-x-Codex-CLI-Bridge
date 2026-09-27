from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_runtime_probe as probe_module  # noqa: E402
from claude_runtime_probe import (  # noqa: E402
    ProbeRefused,
    approval_answer,
    child_env,
    confined_write_target,
    require_subscription,
    run_turn,
    summarize_account,
    summarize_models,
    summarize_rate_limit,
    summarize_result,
)
from fake_claude_cli import SECRET_TEXT  # noqa: E402

FAKE_CLI = [sys.executable, str(Path(__file__).resolve().parent / "fake_claude_cli.py")]
SENSITIVE = ("sk-ant-PLANTED", "someone@example.com", "org-PLANTED", "PLANTED")


class FakeSession:
    """Replays recorded stream-json events and captures what the host sends."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.sent: list[dict] = []

    def user(self, text: str) -> None:
        self.sent.append({"type": "user", "text": text})

    def control(self, request_id: str, request: dict) -> None:
        self.sent.append({"type": "control_request", "request_id": request_id, "request": request})

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def __iter__(self):
        return iter(self.events)


def delta(kind: str = "text_delta") -> dict:
    return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": kind}}}


def result(**fields) -> dict:
    base = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s",
            "permission_denials": [], "modelUsage": {"claude-haiku-4-5-20251001": {}},
            "result": SECRET_TEXT, "terminal_reason": "completed", "api_error_status": None}
    base.update(fields)
    return base


def write_request(path: str, tool: str = "Write") -> dict:
    return {"subtype": "can_use_tool", "tool_name": tool, "input": {"file_path": path, "content": "hi"}}


class WriteConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs = [tempfile.TemporaryDirectory() for _ in range(2)]
        self.root = Path(self._dirs[0].name).resolve()
        self.outside = Path(self._dirs[1].name).resolve()

    def tearDown(self) -> None:
        for handle in self._dirs:
            handle.cleanup()

    def target(self, path: str) -> Path | None:
        return confined_write_target({"file_path": path}, self.root)

    def test_relative_and_absolute_paths_inside_root_are_accepted(self) -> None:
        self.assertEqual(self.target("allow.txt"), self.root / "allow.txt")
        self.assertEqual(self.target(str(self.root / "sub" / "a.txt")), self.root / "sub" / "a.txt")

    def test_absolute_path_outside_root_is_rejected(self) -> None:
        self.assertIsNone(self.target(str(self.outside / "escape.txt")))
        self.assertIsNone(self.target(str(Path.home() / ".bashrc")))

    def test_traversal_is_rejected_in_every_spelling(self) -> None:
        for path in ("../escape.txt", "sub/../../escape.txt", "..\\escape.txt",
                     str(self.root / ".." / "escape.txt"), "sub/../inside.txt"):
            with self.subTest(path=path):
                self.assertIsNone(self.target(path))

    def test_links_that_escape_are_rejected(self) -> None:
        try:
            (self.root / "dirlink").symlink_to(self.outside, target_is_directory=True)
            (self.outside / "victim.txt").write_text("keep", encoding="utf-8")
            (self.root / "filelink.txt").symlink_to(self.outside / "victim.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        self.assertIsNone(self.target("dirlink/escape.txt"))
        self.assertIsNone(self.target("filelink.txt"))

    def test_root_itself_directories_and_malformed_input_are_rejected(self) -> None:
        (self.root / "adir").mkdir()
        self.assertIsNone(self.target(str(self.root)))
        self.assertIsNone(self.target("adir"))
        self.assertIsNone(self.target(""))
        self.assertIsNone(self.target("a\x00b"))
        self.assertIsNone(confined_write_target({"file_path": 7}, self.root))
        self.assertIsNone(confined_write_target("allow.txt", self.root))

    def test_allow_decision_denies_escapes_and_other_tools(self) -> None:
        for request in (write_request(str(self.outside / "x.txt")), write_request("../x.txt"),
                        write_request("ok.txt", tool="Bash"), write_request("ok.txt", tool="Edit")):
            with self.subTest(request=request):
                answer, rejected = approval_answer(request, "allow", self.root)
                self.assertEqual(answer["behavior"], "deny")
                self.assertTrue(rejected)
        answer, rejected = approval_answer(write_request("ok.txt"), "allow", None)
        self.assertEqual(answer["behavior"], "deny")

    def test_allow_pins_the_validated_absolute_path(self) -> None:
        answer, rejected = approval_answer(write_request("ok.txt"), "allow", self.root)
        self.assertFalse(rejected)
        self.assertEqual(answer["updatedInput"]["file_path"], str(self.root / "ok.txt"))

    def test_run_turn_never_allows_an_escaping_write(self) -> None:
        request = {"type": "control_request", "request_id": "r1",
                   "request": write_request(str(self.outside / "escape.txt"))}
        session = FakeSession([request, result()])
        seen, _ = run_turn(session, "write", decision="allow", write_root=self.root)
        self.assertEqual(session.sent[-1]["response"]["response"]["behavior"], "deny")
        self.assertEqual((seen["guard_rejections"], seen["approvals_granted"]), (1, 0))


class BillingEnvironmentTests(unittest.TestCase):
    def test_billing_variables_are_stripped_case_insensitively(self) -> None:
        planted = {"ANTHROPIC_API_KEY": "sk-ant-PLANTED", "anthropic_auth_token": "t",
                   "ANTHROPIC_BASE_URL": "https://gateway.invalid", "CLAUDE_CODE_USE_BEDROCK": "1",
                   "PATH": "/bin", "USERPROFILE": "C:\\Users\\x"}
        env = child_env(planted)
        for name in ("ANTHROPIC_API_KEY", "anthropic_auth_token", "ANTHROPIC_BASE_URL",
                     "CLAUDE_CODE_USE_BEDROCK"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")

    def test_subscription_gate_refuses_non_subscription_auth(self) -> None:
        good_auth = {"logged_in": True, "exit_code": 0, "api_provider": "firstParty"}
        good_account = {"plan": "Claude Max", "api_provider": "firstParty", "api_key_source_present": False}
        require_subscription(good_auth, good_account)
        cases = {
            "not_logged_in": ({**good_auth, "logged_in": False}, good_account),
            "not_first_party": ({**good_auth, "api_provider": "bedrock"}, good_account),
            "not_a_subscription_plan": (good_auth, {**good_account, "plan": "Claude API"}),
            "api_key_source_present": (good_auth, {**good_account, "api_key_source_present": True}),
        }
        for reason, (auth, account) in cases.items():
            with self.subTest(reason=reason), self.assertRaises(ProbeRefused) as caught:
                require_subscription(auth, account)
            self.assertEqual(str(caught.exception), reason)


class RedactionTests(unittest.TestCase):
    def test_account_identifiers_never_survive(self) -> None:
        summary = summarize_account({"email": "someone@example.com", "organization": "Org",
                                     "subscriptionType": "Claude Max", "apiProvider": "firstParty",
                                     "apiKeySource": "ANTHROPIC_API_KEY"})
        self.assertEqual(summary, {"plan": "Claude Max", "api_provider": "firstParty",
                                   "api_key_source_present": True})

    def test_model_menu_rejects_free_text(self) -> None:
        models = [{"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable",
                   "description": SECRET_TEXT, "supportedEffortLevels": ["low", "max", SECRET_TEXT]},
                  {"value": SECRET_TEXT, "resolvedModel": "claude-opus-5-5", "displayName": SECRET_TEXT}]
        summary = summarize_models(models)
        self.assertEqual(summary[0], {"value": "fable", "resolved_model": "claude-fable-5-1",
                                      "display_name": "Fable", "effort_levels": ["low", "max"]})
        self.assertEqual(summary[1]["value"], "<unrecognized>")
        self.assertNotIn("PLANTED", json.dumps(summary))

    def test_rate_limit_summary_keeps_only_numbers_and_enums(self) -> None:
        info = {"status": "allowed_warning", "rateLimitType": "five_hour", "note": SECRET_TEXT,
                "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": 1, "x": SECRET_TEXT}}}
        summary = summarize_rate_limit(info)
        self.assertEqual(summary["windows"]["five_hour"], {"utilization": 0.85, "resets_at": 1})
        self.assertEqual(summary["status"], "allowed_warning")
        self.assertNotIn("PLANTED", json.dumps(summary))

    def test_result_summary_never_contains_the_reply(self) -> None:
        summary = summarize_result(result(is_error=True, api_error_status=404,
                                          terminal_reason="api_error", modelUsage={}))
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["api_error_status"], 404)
        self.assertNotIn("PLANTED", json.dumps(summary))

    def test_missing_or_non_boolean_is_error_is_a_failure(self) -> None:
        self.assertFalse(summarize_result({"type": "result"})["ok"])
        self.assertFalse(summarize_result({"is_error": "false"})["ok"])


class TurnTests(unittest.TestCase):
    def test_streams_text_and_records_rate_limit(self) -> None:
        session = FakeSession([delta("thinking_delta"), delta(), delta(),
                               {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
                               result()])
        seen, reply = run_turn(session, "hi")
        self.assertEqual(seen["text_deltas"], 2)
        self.assertEqual(seen["rate_limit"]["status"], "allowed")
        self.assertTrue(seen["ok"])
        self.assertEqual(reply, SECRET_TEXT)
        self.assertNotIn("PLANTED", json.dumps(seen))

    def test_deny_decision_denies_even_a_safe_write(self) -> None:
        request = {"type": "control_request", "request_id": "r1", "request": write_request("a.txt")}
        session = FakeSession([request, result()])
        with tempfile.TemporaryDirectory() as root:
            run_turn(session, "write", decision="deny", write_root=Path(root))
        answer = session.sent[-1]["response"]
        self.assertEqual((answer["request_id"], answer["response"]["behavior"]), ("r1", "deny"))

    def test_interrupt_is_sent_once_after_the_requested_delta(self) -> None:
        session = FakeSession([delta(), delta(), delta(),
                               result(subtype="error_during_execution", is_error=True,
                                      terminal_reason="aborted_streaming")])
        seen, _ = run_turn(session, "count", interrupt_after=2)
        interrupts = [m for m in session.sent if m.get("request", {}).get("subtype") == "interrupt"]
        self.assertEqual(len(interrupts), 1)
        self.assertEqual((seen["ok"], seen["terminal_reason"]), (False, "aborted_streaming"))

    def test_refusal_fallback_notice_is_surfaced_without_its_explanation(self) -> None:
        notice = {"type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal",
                  "original_model": "claude-fable-5", "fallback_model": "claude-opus-4-8",
                  "api_refusal_explanation": SECRET_TEXT}
        seen, _ = run_turn(FakeSession([notice, result()]), "hi")
        self.assertEqual(seen["notices"], [{"subtype": "model_refusal_fallback",
                                            "original_model": "claude-fable-5",
                                            "fallback_model": "claude-opus-4-8",
                                            "trigger": "refusal"}])

    def test_stream_without_result_is_a_failure(self) -> None:
        seen, _ = run_turn(FakeSession([delta()]), "hi")
        self.assertFalse(seen["ok"])


class EndToEndFakeCliTests(unittest.TestCase):
    """Runs the real probe against fake_claude_cli.py; no model is contacted."""

    def run_probe(self, turns: bool, plan: str = "Claude Max", model: str = "haiku",
                  extra_env: dict | None = None) -> tuple[dict, str, list[dict]]:
        with tempfile.TemporaryDirectory() as logs:
            log_path = Path(logs) / "calls.jsonl"
            env = {"FAKE_CLAUDE_LOG": str(log_path), "FAKE_PLAN": plan,
                   "ANTHROPIC_API_KEY": "sk-ant-PLANTED-KEY", "anthropic_base_url": "https://x.invalid",
                   **(extra_env or {})}
            stdout = io.StringIO()
            with patch.dict(os.environ, env), redirect_stdout(stdout), \
                    patch.object(probe_module, "resolve_claude", return_value=FAKE_CLI):
                code = probe_module.main((["--turns"] if turns else []) + ["--model", model])
            calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        output = stdout.getvalue()
        report = json.loads(output)
        report["_exit"] = code
        return report, output, calls

    def assert_nothing_sensitive(self, output: str) -> None:
        for marker in SENSITIVE:
            self.assertNotIn(marker, output)

    def test_planted_api_key_never_reaches_any_child(self) -> None:
        _, _, calls = self.run_probe(turns=True)
        spawned = [call for call in calls if "argv" in call]
        self.assertGreaterEqual(len(spawned), 8)  # version, auth and every stream session
        for call in spawned:
            self.assertEqual(call["billing_vars_seen"], [], call["argv"])

    def test_inventory_stdout_holds_no_account_or_model_text(self) -> None:
        report, output, calls = self.run_probe(turns=False)
        self.assert_nothing_sensitive(output)
        self.assertEqual(report["account"]["plan"], "Claude Max")
        self.assertFalse(any(call.get("user_turn") for call in calls))

    def test_api_plan_refuses_turns_before_any_model_call(self) -> None:
        report, output, calls = self.run_probe(turns=True, plan="Claude API")
        self.assertEqual(report["turns_refused"], "not_a_subscription_plan")
        self.assertEqual(report["_exit"], 2)
        self.assertFalse(any(call.get("user_turn") for call in calls))
        self.assert_nothing_sensitive(output)

    def test_credit_billed_model_is_refused(self) -> None:
        report, _, calls = self.run_probe(turns=True, model="fable")
        self.assertEqual(report["turns_refused"], "model_may_bill_usage_credits")

    def test_full_turn_run_reports_structure_only(self) -> None:
        report, output, _ = self.run_probe(turns=True)
        self.assert_nothing_sensitive(output)
        self.assertTrue(report["resume"]["recalled"])
        self.assertTrue(report["approval_allow"]["file_written"])
        self.assertFalse(report["approval_deny"]["file_written"])
        self.assertEqual(report["interrupt"]["terminal_reason"], "aborted_streaming")
        self.assertEqual(report["unknown_model"]["api_error_status"], 404)
        self.assertEqual(report["stream_turn"]["notices"][0]["fallback_model"], "claude-opus-4-8")

    def test_escaping_write_proposed_by_the_cli_is_denied_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            victim = Path(outside) / "escape.txt"
            report, _, _ = self.run_probe(turns=True, extra_env={"FAKE_WRITE_TARGET": str(victim)})
            self.assertFalse(victim.exists())
        self.assertEqual(report["approval_allow"]["guard_rejections"], 1)
        self.assertEqual(report["approval_allow"]["approvals_granted"], 0)


if __name__ == "__main__":
    unittest.main()
