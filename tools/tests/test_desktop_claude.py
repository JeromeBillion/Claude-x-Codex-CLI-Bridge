"""The shared desktop host driving the Claude adapter, against the fake CLI (no Tk window, no model)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_claude_runtime import FakeCliFixture  # noqa: E402
from tools import claude_runtime  # noqa: E402
from tools.approval_modes import TrustedFolderStore  # noqa: E402
from tools.desktop import DesktopHost  # noqa: E402
from tools.conversation import ConversationLog  # noqa: E402


class DesktopClaudeTests(FakeCliFixture):
    def host(self) -> DesktopHost:
        host = DesktopHost.__new__(DesktopHost)  # the Tk window is not needed for turn routing
        host.messages = queue.Queue()
        host.history = []
        host.workspace = self.root.resolve()
        host.claude_preflight = self.pre
        host.claude_session = None
        host.claude_model = None
        host.claude_consent = None
        host.conversation = None  # VLI-160 recording is covered in test_desktop_conversation
        host.claude_trust = self.trust
        host.codex_trust = TrustedFolderStore(self.root / "auto.json")
        host.asked = []
        host._ask = lambda kind, data: host.asked.append((kind, data.data["policy"])) or False
        self.addCleanup(lambda: host.claude_session and host.claude_session.close())
        return host

    def restarted_host(self) -> DesktopHost:
        """A fresh host (no live Claude process) that reopened the saved conversation log."""
        host = self.host()
        host.claude_unresumable = set()
        host.conversation = ConversationLog(self.root / "conversation.jsonl", "ws")
        return host

    def session_spawns(self) -> list[list[str]]:
        return [entry["argv"] for entry in map(json.loads, self.log.read_text(encoding="utf-8").splitlines())
                if "--permission-prompt-tool" in entry.get("argv", [])]

    def test_a_restarted_window_resumes_the_conversations_claude_session(self) -> None:
        first = self.restarted_host()
        first._claude_turn("Reply OK", "sonnet", "answer")
        native = first.claude_session.session_ref
        first.claude_session.close()
        second = self.restarted_host()  # window closed and reopened: nothing in memory
        self.assertTrue(second._claude_turn("Reply OK", "sonnet", "answer").ok)
        argv = self.session_spawns()[-1]
        self.assertEqual(argv[argv.index("--resume") + 1], native)

    def test_a_saved_session_the_cli_lost_starts_fresh_once_and_is_not_retried(self) -> None:
        first = self.restarted_host()
        first._claude_turn("Reply OK", "sonnet", "answer")
        native = first.claude_session.session_ref
        first.claude_session.close()
        second = self.restarted_host()
        with patch.dict(os.environ, {"FAKE_MISSING_SESSION": native}):
            self.assertTrue(second._claude_turn("Reply OK", "sonnet", "answer").ok)
        spawns = self.session_spawns()
        self.assertIn("--resume", spawns[-2])
        self.assertNotIn("--resume", spawns[-1])
        self.assertIn(native, second.claude_unresumable)
        self.assertIn("could not be resumed", "".join(str(item[1]) for item in list(second.messages.queue)))

    def test_a_lost_fable_session_asks_for_consent_again_instead_of_reusing_it(self) -> None:
        first = self.restarted_host()
        first.claude_consent = claude_runtime.CreditConsent(confirmed_by_user=True)
        first._claude_turn("Reply OK", "claude-fable-5[1m]", "answer")
        native = first.claude_session.session_ref
        first.claude_session.close()
        second = self.restarted_host()
        second.claude_consent = claude_runtime.CreditConsent(confirmed_by_user=True)
        with patch.dict(os.environ, {"FAKE_MISSING_SESSION": native}):
            with self.assertRaises(claude_runtime.RuntimeRefused) as refused:
                second._claude_turn("Reply OK", "claude-fable-5[1m]", "answer")
        self.assertEqual(str(refused.exception), "saved_session_not_resumable")
        self.assertIsNone(second.claude_consent)
        self.assertIsNone(second._saved_claude_session())  # the next send starts fresh, after a new consent

    def test_claude_auto_accept_in_a_trusted_folder_does_not_block_on_the_user(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        host = self.host()
        host.codex_trust.trust(self.root)
        turn = host._claude_turn("EDIT a.txt", "sonnet", "answer", "auto_accept_trusted")
        self.assertTrue(turn.ok)
        self.assertEqual(host.asked, [])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "b")

    def test_ask_every_edit_asks_the_user_and_honours_a_decline(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        host = self.host()
        host._claude_turn("EDIT a.txt", "sonnet", "answer", "ask_every_edit")
        self.assertEqual(host.asked, [("claude_approval", "ask")])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "a")

    def test_switching_the_toggle_between_turns_reuses_the_native_session(self) -> None:
        host = self.host()
        host.codex_trust.trust(self.root)
        host._claude_turn("Reply OK", "sonnet", "answer", "ask_every_edit")
        first = host.claude_session
        host._claude_turn("Reply OK", "sonnet", "answer", "auto_accept_trusted")
        self.assertIs(host.claude_session, first)
        self.assertEqual(first.approval_mode, "auto_accept_trusted")

    def test_fable_consent_is_consumed_by_exactly_one_new_session(self) -> None:
        host = self.host()
        with self.assertRaises(claude_runtime.RuntimeRefused):
            host._claude_turn("Reply OK", "claude-fable-5[1m]", "answer")
        host.claude_consent = claude_runtime.CreditConsent(confirmed_by_user=True)
        self.assertTrue(host._claude_turn("Reply OK", "claude-fable-5[1m]", "answer").ok)
        self.assertIsNone(host.claude_consent)
        host._claude_turn("Reply OK", "sonnet", "answer")  # model change: a new process session
        with self.assertRaises(claude_runtime.RuntimeRefused):
            host._claude_turn("Reply OK", "claude-fable-5[1m]", "answer")

    def test_an_unused_consent_never_carries_over_to_a_later_send(self) -> None:
        host = self.host()
        host.claude_consent = claude_runtime.CreditConsent(confirmed_by_user=True, model="claude-fable-5[1m]")
        with patch.object(DesktopHost, "_route_turn", side_effect=RuntimeError("collaboration failed")):
            with self.assertRaises(RuntimeError):
                host._run_turn("collaboration", "ask_every_edit", "p", "m", "", "claude-fable-5[1m]")
        self.assertIsNone(host.claude_consent)

    def test_a_default_that_resolves_to_fable_counts_as_credit_billed(self) -> None:
        host = self.host()
        host.claude_preflight = claude_runtime.Preflight(
            self.pre.executable, self.pre.version, self.pre.version_status, self.pre.auth, self.pre.account,
            claude_runtime.local_menu([{"value": "default", "resolvedModel": "claude-fable-5-1"}]), [])
        self.assertTrue(host._claude_credit_billed("default"))
        self.assertFalse(host._claude_credit_billed("sonnet"))

    def test_a_runtime_error_fails_the_turn_without_leaking_into_the_next(self) -> None:
        host = self.host()
        failed = host._claude_turn("BILLING ERROR", "sonnet", "answer")
        self.assertEqual((failed.ok, failed.failure), (False, "billing"))
        following = host._claude_turn("Reply OK", "sonnet", "answer")
        self.assertTrue(following.ok)
        self.assertEqual([e.kind for e in following.events][-1], "turn_finished")

    def test_session_process_billing_refusal_surfaces_as_a_reason_code(self) -> None:
        host = self.host()
        with patch.dict(os.environ, {"FAKE_SESSION_PLAN": "Claude API"}), \
                self.assertRaises(claude_runtime.RuntimeRefused) as caught:
            host._claude_turn("Reply OK", "sonnet", "answer")
        self.assertEqual(str(caught.exception), "not_a_subscription_plan")


if __name__ == "__main__":
    unittest.main()
