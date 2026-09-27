from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
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
    summarize_auth,
    summarize_models,
    summarize_notice,
    summarize_rate_limit,
    summarize_result,
)
from fake_claude_cli import MARKER, SECRET_TEXT  # noqa: E402

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


# Every provider credential or billing variable we know of, with a planted value.
PLANTED_ENV = {
    name: f"PLANTED-{name}"
    for names in probe_module.STRIPPED_CATEGORIES.values() for name in names
}
PLANTED_ENV.update({
    "anthropic_api_key": "PLANTED-lowercase",  # Windows env names are case-insensitive
    "SOME_FUTURE_CLOUD_SECRET": "PLANTED-unknown",  # not on any list: the allowlist must drop it
    "ARM_CLIENT_SECRET": "PLANTED-terraform-azure",
    "GOOGLE_OAUTH_ACCESS_TOKEN": "PLANTED-gcp-token",
})


class BillingEnvironmentTests(unittest.TestCase):
    def test_every_credential_category_is_dropped_and_plumbing_kept(self) -> None:
        base = {**PLANTED_ENV, "PATH": "/bin", "USERPROFILE": "C:\\Users\\x", "HTTPS_PROXY": "http://p",
                "SystemRoot": "C:\\Windows", "JAVA_HOME": "/opt/java"}
        env = child_env(base)
        self.assertEqual({name for name in env if "PLANTED" in env[name]}, set())
        for name in PLANTED_ENV:
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["USERPROFILE"], "C:\\Users\\x")
        self.assertEqual(env["HTTPS_PROXY"], "http://p")
        self.assertEqual(env["SystemRoot"], "C:\\Windows")  # allowlist matches case-insensitively
        self.assertNotIn("JAVA_HOME", env)  # the probe needs no toolchains
        self.assertEqual(env["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")

    def test_categories_cover_the_prompt_3_examples(self) -> None:
        listed = {name for names in probe_module.STRIPPED_CATEGORIES.values() for name in names}
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                     "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertIn(name, listed)
            self.assertNotIn(name, probe_module.ENV_ALLOWLIST)

    def test_subscription_gate_refuses_non_subscription_auth(self) -> None:
        good_auth = {"logged_in": True, "exit_code": 0, "api_provider": "firstParty",
                     "auth_method": "claude.ai", "api_key_source_present": False}
        good_account = {"plan": "Claude Max", "api_provider": "firstParty", "api_key_source_present": False}
        require_subscription(good_auth, good_account)
        cases = {
            "not_logged_in": ({**good_auth, "logged_in": False}, good_account),
            "not_a_claude_ai_login": ({**good_auth, "auth_method": "oauth_token"}, good_account),
            "not_first_party": ({**good_auth, "api_provider": "bedrock"}, good_account),
            "not_a_subscription_plan": (good_auth, {**good_account, "plan": "Claude API"}),
            "api_key_source_present": ({**good_auth, "api_key_source_present": True}, good_account),
        }
        for reason, (auth, account) in cases.items():
            with self.subTest(reason=reason), self.assertRaises(ProbeRefused) as caught:
                require_subscription(auth, account)
            self.assertEqual(str(caught.exception), reason)
        with self.assertRaises(ProbeRefused):
            require_subscription(good_auth, {**good_account, "plan": "unlisted"})


class RedactionTests(unittest.TestCase):
    def test_account_identifiers_never_survive(self) -> None:
        summary = summarize_account({"email": "someone@example.com", "organization": "Org",
                                     "subscriptionType": "Claude Max", "apiProvider": "firstParty",
                                     "apiKeySource": "ANTHROPIC_API_KEY"})
        self.assertEqual(summary, {"plan": "Claude Max", "api_provider": "firstParty",
                                   "api_key_source_present": True})

    def test_short_marker_shaped_like_an_enum_is_never_echoed(self) -> None:
        # Each value would have passed the old regex shape checks.
        self.assertEqual(summarize_auth({"authMethod": MARKER, "apiProvider": MARKER}, 0)["auth_method"], "unlisted")
        self.assertEqual(summarize_account({"subscriptionType": MARKER, "apiProvider": MARKER}),
                         {"plan": "unlisted", "api_provider": "unlisted", "api_key_source_present": False})
        rate = summarize_rate_limit({"status": MARKER, "rateLimitType": MARKER, "overageStatus": MARKER,
                                     "unifiedWindows": {MARKER: {"utilization": 0.5}}})
        self.assertNotIn(MARKER, json.dumps(rate))
        self.assertEqual(rate["unlisted_windows"], 1)
        notice = summarize_notice({"subtype": MARKER, "original_model": MARKER,
                                   "fallback_model": f"claude-opus-{MARKER}", "trigger": MARKER})
        self.assertEqual(notice, {"subtype": "unlisted", "original_model": "unlisted",
                                  "fallback_model": "unlisted-claude-opus", "trigger": "unlisted"})
        result_summary = summarize_result({"is_error": False, "subtype": MARKER, "terminal_reason": MARKER,
                                           "api_error_status": 12345, "modelUsage": {MARKER: {}}})
        self.assertNotIn(MARKER, json.dumps(result_summary))
        self.assertIsNone(result_summary["api_error_status"])

    def test_model_menu_keeps_public_names_and_buckets_the_rest(self) -> None:
        models = [{"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable",
                   "description": SECRET_TEXT, "supportedEffortLevels": ["max", "low", SECRET_TEXT]},
                  {"value": SECRET_TEXT, "resolvedModel": "claude-sonnet-9-priv8x7q", "displayName": MARKER},
                  {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Opus"}]
        summary = summarize_models(models)
        self.assertEqual(summary[0], {"value": "fable", "resolved_model": "claude-fable-5-1",
                                      "display_name": "Fable", "effort_levels": ["low", "max"]})
        self.assertEqual(summary[1], {"value": "unlisted", "resolved_model": "unlisted-claude-sonnet",
                                      "display_name": "unlisted", "effort_levels": []})
        self.assertEqual(summary[2]["resolved_model"], "claude-opus-5-5[1m]")
        self.assertNotIn("PLANTED", json.dumps(summary))
        self.assertNotIn(MARKER, json.dumps(summary))

    def test_numbers_are_bounded(self) -> None:
        rate = summarize_rate_limit({"unifiedWindows": {"five_hour": {"utilization": 0.8567, "resetsAt": 1790520600},
                                                        "seven_day": {"utilization": 1e9, "resetsAt": 42}}})
        self.assertEqual(rate["windows"]["five_hour"], {"utilization": 0.86, "resets_at": 1790520600})
        self.assertEqual(rate["windows"]["seven_day"], {"utilization": None, "resets_at": None})

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
                  extra_env: dict | None = None,
                  claude_command: str | None = None) -> tuple[dict, str, list[dict]]:
        with tempfile.TemporaryDirectory() as logs:
            log_path = Path(logs) / "calls.jsonl"
            env = {**PLANTED_ENV, "FAKE_CLAUDE_LOG": str(log_path), "FAKE_PLAN": plan, **(extra_env or {})}
            # Test plumbing for the fake CLI is the only addition to the real allowlist.
            allowlist = probe_module.ENV_ALLOWLIST | {"FAKE_CLAUDE_LOG", "FAKE_PLAN", "FAKE_WRITE_TARGET",
                                                      "FAKE_MARKER"}
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = (["--turns"] if turns else []) + ["--model", model]
            with patch.dict(os.environ, env), redirect_stdout(stdout), redirect_stderr(stderr), \
                    patch.object(probe_module, "ENV_ALLOWLIST", allowlist), \
                    patch.object(probe_module, "resolve_claude",
                                 side_effect=None if claude_command is None else probe_module.CliNotFound,
                                 return_value=FAKE_CLI):
                code = probe_module.main(argv)
            calls = ([json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
                     if log_path.exists() else [])
        output = stdout.getvalue() + stderr.getvalue()
        report = json.loads(stdout.getvalue())
        report["_exit"] = code
        return report, output, calls

    def assert_nothing_sensitive(self, output: str) -> None:
        for marker in SENSITIVE:
            self.assertNotIn(marker, output)

    def test_no_planted_credential_reaches_any_child(self) -> None:
        _, _, calls = self.run_probe(turns=True)
        spawned = [call for call in calls if "argv" in call]
        self.assertGreaterEqual(len(spawned), 8)  # version, auth and every stream session
        for call in spawned:
            self.assertEqual(call["env_values_with_planted"], [], call["argv"])
            leaked = {name.upper() for name in call["env_names"]} & {name.upper() for name in PLANTED_ENV}
            self.assertEqual(leaked, set(), call["argv"])

    def test_inventory_stdout_holds_no_account_or_model_text(self) -> None:
        report, output, calls = self.run_probe(turns=False)
        self.assert_nothing_sensitive(output)
        self.assertEqual(report["account"]["plan"], "Claude Max")
        self.assertEqual(report["auth"]["auth_method"], "claude.ai")
        self.assertFalse(any(call.get("user_turn") for call in calls))

    def test_marker_in_every_cli_field_never_reaches_output(self) -> None:
        report, output, calls = self.run_probe(turns=True, extra_env={"FAKE_MARKER": "all"})
        self.assertNotIn(MARKER, output)
        self.assert_nothing_sensitive(output)
        # Gating fields were marked, so the gate must fail closed with no model turn.
        self.assertEqual(report["turns_refused"], "not_a_claude_ai_login")
        self.assertFalse(any(call.get("user_turn") for call in calls))
        self.assertEqual(report["auth"]["auth_method"], "unlisted")
        self.assertEqual(report["version"], "2.1.283")

    def test_marker_in_turn_fields_never_reaches_output_but_diagnostics_remain(self) -> None:
        report, output, _ = self.run_probe(turns=True, extra_env={"FAKE_MARKER": "turns"})
        self.assertNotIn(MARKER, output)
        self.assert_nothing_sensitive(output)
        turn = report["stream_turn"]
        self.assertTrue(turn["ok"])
        self.assertEqual(turn["subtype"], "unlisted")
        self.assertIn("claude-haiku-4-5-20251001", turn["models_served"])
        self.assertIn("unlisted-claude-opus", turn["models_served"])
        self.assertEqual(turn["rate_limit"]["windows"]["five_hour"]["utilization"], 0.1)
        self.assertEqual(turn["rate_limit"]["unlisted_windows"], 1)
        self.assertEqual(turn["notices"][0]["subtype"], "model_refusal_fallback")
        self.assertEqual(report["permission_mode"], "unlisted")
        self.assertEqual(report["models"][-1]["resolved_model"], "unlisted-claude-opus")

    def test_api_plan_refuses_turns_before_any_model_call(self) -> None:
        report, output, calls = self.run_probe(turns=True, plan="Claude API")
        self.assertEqual(report["turns_refused"], "not_a_subscription_plan")
        self.assertEqual(report["_exit"], 2)
        self.assertFalse(any(call.get("user_turn") for call in calls))
        self.assert_nothing_sensitive(output)

    def test_only_cheap_plan_models_are_accepted(self) -> None:
        for model in ("fable", "best", "opus", "claude-fable-5-1"):
            with self.subTest(model=model), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe_module.main(["--turns", "--model", model])

    def test_errors_are_reported_as_fixed_codes_only(self) -> None:
        report, output, _ = self.run_probe(turns=False, claude_command="missing")
        self.assertEqual(report, {"error": "claude_not_found", "_exit": 3})
        with patch.object(probe_module, "resolve_claude", return_value=FAKE_CLI), \
                patch.object(probe_module, "probe", side_effect=RuntimeError(f"boom {SECRET_TEXT} {MARKER}")), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(probe_module.main([]), 3)
        self.assertEqual(json.loads(out.getvalue()), {"error": "internal_error"})
        self.assertNotIn(MARKER, out.getvalue() + err.getvalue())
        self.assert_nothing_sensitive(out.getvalue() + err.getvalue())

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
