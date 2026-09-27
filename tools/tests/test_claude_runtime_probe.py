from __future__ import annotations

from pathlib import Path
import sys
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from claude_runtime_probe import (  # noqa: E402
    pick,
    run_turn,
    summarize_models,
    summarize_rate_limit,
    summarize_result,
)


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
            "result": "OK", "terminal_reason": "completed", "api_error_status": None}
    base.update(fields)
    return base


class RedactionTests(unittest.TestCase):
    def test_account_identifiers_never_survive_the_whitelist(self) -> None:
        account = {"email": "someone@example.com", "organization": "Org", "subscriptionType": "max",
                   "apiProvider": "firstParty"}
        self.assertEqual(pick(account, ("subscriptionType", "apiProvider")),
                         {"subscriptionType": "max", "apiProvider": "firstParty"})

    def test_model_menu_keeps_only_selection_fields(self) -> None:
        models = [{"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable",
                   "description": "long text", "supportedEffortLevels": ["low", "max"]}]
        self.assertEqual(summarize_models(models), [
            {"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable",
             "supportedEffortLevels": ["low", "max"]}])

    def test_rate_limit_summary_keeps_windows(self) -> None:
        info = {"status": "allowed_warning", "rateLimitType": "five_hour", "isUsingOverage": False,
                "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": 1}}}
        self.assertEqual(summarize_rate_limit(info)["windows"]["five_hour"]["utilization"], 0.85)
        self.assertEqual(summarize_rate_limit(info)["status"], "allowed_warning")


class ResultTests(unittest.TestCase):
    def test_is_error_wins_over_a_success_subtype(self) -> None:
        # Observed from CLI 2.1.283 for an unknown model: subtype "success", is_error true, 404.
        summary = summarize_result(result(is_error=True, api_error_status=404,
                                          terminal_reason="api_error", modelUsage={}))
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["api_error_status"], 404)

    def test_missing_is_error_is_treated_as_failure(self) -> None:
        self.assertFalse(summarize_result({"type": "result"})["ok"])


class TurnTests(unittest.TestCase):
    def test_streams_text_and_records_rate_limit(self) -> None:
        session = FakeSession([delta("thinking_delta"), delta(), delta(),
                               {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
                               result()])
        seen = run_turn(session, "hi")
        self.assertEqual(seen["text_deltas"], 2)
        self.assertEqual(seen["rate_limit"], {"status": "allowed"})
        self.assertTrue(seen["ok"])

    def test_approval_request_is_answered_with_the_hosts_decision(self) -> None:
        request = {"type": "control_request", "request_id": "r1",
                   "request": {"subtype": "can_use_tool", "tool_name": "Write",
                               "input": {"file_path": "a.txt", "content": "hi"}}}
        for decision, behavior in (("allow", "allow"), ("deny", "deny")):
            session = FakeSession([request, result()])
            seen = run_turn(session, "write", decision=decision)
            answer = session.sent[-1]["response"]
            self.assertEqual(answer["request_id"], "r1")
            self.assertEqual(answer["response"]["behavior"], behavior)
            self.assertEqual(seen["approval_requests"], ["Write"])
        self.assertNotIn("updatedInput", answer["response"])

    def test_interrupt_is_sent_after_the_requested_delta(self) -> None:
        session = FakeSession([delta(), delta(), delta(),
                               result(subtype="error_during_execution", is_error=True,
                                      terminal_reason="aborted_streaming")])
        seen = run_turn(session, "count", interrupt_after=2)
        interrupts = [m for m in session.sent if m.get("request", {}).get("subtype") == "interrupt"]
        self.assertEqual(len(interrupts), 1)
        self.assertFalse(seen["ok"])
        self.assertEqual(seen["terminal_reason"], "aborted_streaming")

    def test_refusal_fallback_notice_is_surfaced(self) -> None:
        notice = {"type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal",
                  "original_model": "claude-fable-5", "fallback_model": "claude-opus-4-8",
                  "api_refusal_explanation": "withheld"}
        seen = run_turn(FakeSession([notice, result()]), "hi")
        self.assertEqual(seen["notices"], [{"subtype": "model_refusal_fallback",
                                            "original_model": "claude-fable-5",
                                            "fallback_model": "claude-opus-4-8",
                                            "trigger": "refusal"}])

    def test_stream_without_result_is_a_failure(self) -> None:
        seen = run_turn(FakeSession([delta()]), "hi")
        self.assertFalse(seen["ok"])


if __name__ == "__main__":
    unittest.main()
