"""Collaboration and handoff rules independent of provider subscriptions."""

import unittest
from unittest.mock import patch

from tools.desktop import DesktopHost
from tools.desktop_state import Collaboration, TurnRecord, handoff_text
from tools.runtime_events import Envelope


class DesktopStateTests(unittest.TestCase):
    def test_default_codex_gate_declines_escalation_even_if_user_changes_toggle(self):
        request = Envelope("codex", "native", "approval_request", {"family": "command", "command": "write file"})
        with patch("tools.desktop.messagebox.showwarning") as warning, patch("tools.desktop.messagebox.askyesno") as accept:
            decision = DesktopHost._dialog(object(), "codex_approval", (request, "ask_every_edit"))
        self.assertEqual(decision, "decline")
        warning.assert_called_once()
        accept.assert_not_called()

    def test_single_turn_keeps_provider_ownership_and_failure(self):
        turn = TurnRecord("codex", "catalog-model")
        turn.append(Envelope("codex", "native-thread", "text_delta", {"text": "draft"}))
        turn.append(Envelope("codex", "native-thread", "turn_finished", {"ok": False, "terminal_reason": "limit"}))
        self.assertEqual(turn.text, "draft")
        self.assertEqual(turn.failure, "limit")
        self.assertFalse(turn.ok)
        with self.assertRaises(ValueError):
            turn.append(Envelope("claude", "other-session", "text_delta", {"text": "wrong"}))

    def test_joint_result_requires_two_exact_votes_and_resets_for_new_candidate(self):
        state = Collaboration()
        state.propose("candidate")
        state.vote("claude", approves=True, reviewed_text="candidate")
        self.assertIsNone(state.joint_answer)
        state.vote("codex", approves=True, reviewed_text="candidate")
        self.assertEqual(state.joint_answer, "candidate")
        state.propose("revised")
        self.assertIsNone(state.joint_answer)
        state.vote("claude", approves=True, reviewed_text="different")
        self.assertEqual(state.state, "needs_user_decision")
        self.assertIsNone(state.joint_answer)

    def test_failed_handoff_is_explicit_and_native_session_is_not_transferred(self):
        turn = TurnRecord("codex", "catalog-model")
        turn.append(Envelope("codex", "native-thread", "text_delta", {"text": "evidence"}))
        turn.append(Envelope("codex", "native-thread", "turn_finished", {"ok": False}))
        packet = handoff_text("workspace", turn, "Review this", ("a.py",))
        self.assertIn("failed or incomplete", packet)
        self.assertIn("cannot resume in the other provider", packet)
        self.assertIn("a.py", packet)
        with self.assertRaises(ValueError):
            handoff_text("workspace", turn, "")


if __name__ == "__main__":
    unittest.main()
