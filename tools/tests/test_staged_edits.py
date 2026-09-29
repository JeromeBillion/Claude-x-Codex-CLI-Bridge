"""Focused tests for the host-owned Codex file-edit boundary."""

import json
import hashlib
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock

from tools.desktop import DesktopHost
from tools.desktop_state import TurnRecord
from tools.runtime_events import Envelope
from tools.staged_edits import EditProposalError, stage_proposals


def proposal(*edits):
    return "Answer\n```codex-edits\n" + json.dumps({"edits": list(edits)}) + "\n```"


class StagedEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_exact_replacement_requires_individual_apply(self):
        target = self.root / "app.py"
        target.write_text("first\nsecond\n", encoding="utf-8")
        staged = stage_proposals(self.root, proposal({"path": "app.py", "old_text": "second", "new_text": "changed"}))
        self.assertEqual(target.read_text(encoding="utf-8"), "first\nsecond\n")
        self.assertIn("-second", staged[0].diff)
        staged[0].apply()
        self.assertEqual(target.read_text(encoding="utf-8"), "first\nchanged\n")

    def test_diff_remains_readable_without_final_newline(self):
        target = self.root / "plain.txt"
        target.write_text("before", encoding="utf-8")
        edit = stage_proposals(self.root, proposal({"path": "plain.txt", "old_text": "before", "new_text": "after"}))[0]
        self.assertIn("-before\n\\ No newline at end of file\n+after", edit.diff)

    def test_stale_file_cannot_be_overwritten_after_review(self):
        target = self.root / "app.py"
        target.write_text("before", encoding="utf-8")
        edit = stage_proposals(self.root, proposal({"path": "app.py", "old_text": "before", "new_text": "after"}))[0]
        target.write_text("new user work", encoding="utf-8")
        with self.assertRaisesRegex(EditProposalError, "changed since review"):
            edit.apply()
        self.assertEqual(target.read_text(encoding="utf-8"), "new user work")

    def test_create_and_delete_require_explicit_exact_state(self):
        create = stage_proposals(self.root, proposal({"path": "new.txt", "old_text": None, "new_text": "hello"}))[0]
        self.assertFalse((self.root / "new.txt").exists())
        create.apply()
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "hello")
        delete = stage_proposals(self.root, proposal({"path": "new.txt", "old_text": "hello", "new_text": None}))[0]
        delete.apply()
        self.assertFalse((self.root / "new.txt").exists())

    def test_rejects_unsafe_paths_and_ambiguous_replacements(self):
        (self.root / "x.txt").write_text("same same", encoding="utf-8")
        for name in ("../escape.txt", "/absolute.txt", ".git/config", "a/.git/config", "C:/windows", "CON.txt", "a\\b"):
            with self.subTest(name=name), self.assertRaises(EditProposalError):
                stage_proposals(self.root, proposal({"path": name, "old_text": None, "new_text": "x"}))
        with self.assertRaisesRegex(EditProposalError, "match exactly once"):
            stage_proposals(self.root, proposal({"path": "x.txt", "old_text": "same", "new_text": "other"}))

    def test_missing_parent_is_a_rejected_proposal_not_an_unhandled_error(self):
        host = DesktopHost.__new__(DesktopHost)
        host.workspace = self.root
        host.messages = queue.Queue()
        host._ask = Mock()
        turn = TurnRecord("codex", "catalog-model", text=proposal(
            {"path": "missing/new.txt", "old_text": None, "new_text": "content"}))
        host._review_codex_edits(turn)
        host._ask.assert_not_called()
        self.assertIn("Edit parent must already exist", host.messages.get_nowait()[1])
        self.assertFalse((self.root / "missing").exists())

    def test_rejects_link_escape(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable on this host")
        with self.assertRaisesRegex(EditProposalError, "link"):
            stage_proposals(self.root, proposal({"path": "link/escape.txt", "old_text": None, "new_text": "x"}))

    def test_rejects_whole_batch_before_any_apply(self):
        (self.root / "valid.txt").write_text("old", encoding="utf-8")
        with self.assertRaises(EditProposalError):
            stage_proposals(self.root, proposal(
                {"path": "valid.txt", "old_text": "old", "new_text": "new"},
                {"path": "../escape.txt", "old_text": None, "new_text": "bad"},
            ))
        self.assertEqual((self.root / "valid.txt").read_text(encoding="utf-8"), "old")

    def test_desktop_requires_separate_yes_for_each_file(self):
        (self.root / "one.txt").write_text("one", encoding="utf-8")
        (self.root / "two.txt").write_text("two", encoding="utf-8")
        host = DesktopHost.__new__(DesktopHost)
        host.workspace = self.root
        host.messages = queue.Queue()
        host._ask = Mock(side_effect=[False, True])
        turn = TurnRecord("codex", "catalog-model", text=proposal(
            {"path": "one.txt", "old_text": "one", "new_text": "changed"},
            {"path": "two.txt", "old_text": "two", "new_text": "changed"},
        ))
        host._review_codex_edits(turn)
        self.assertEqual(host._ask.call_count, 2)
        self.assertEqual((self.root / "one.txt").read_text(encoding="utf-8"), "one")
        self.assertEqual((self.root / "two.txt").read_text(encoding="utf-8"), "changed")

    def test_collaboration_waits_for_both_votes_and_file_consent(self):
        target = self.root / "app.py"
        target.write_text("before", encoding="utf-8")
        candidate = proposal({"path": "app.py", "old_text": "before", "new_text": "after"})
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        host = DesktopHost.__new__(DesktopHost)
        host.workspace = self.root
        host.messages = queue.Queue()
        turns = []

        def finished(provider, text):
            turn = TurnRecord(provider, "menu-model", text=text)
            turn.append(Envelope(provider, "native", "turn_finished", {"ok": True}))
            return turn

        def codex_turn(_text, _model, _effort, _approval, role):
            turns.append(("codex", role))
            return finished("codex", candidate if role.startswith("draft") else f"APPROVE {digest}")

        def claude_turn(_text, _model, role, _approval):
            turns.append(("claude", role))
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            return finished("claude", f"APPROVE {digest}")

        def ask(kind, value):
            if kind == "handoff":
                return value
            self.assertEqual(kind, "edit_review")
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertIn("-before", value.diff)
            self.assertEqual(turns[-1], ("codex", "final review, unapproved"))
            return True

        host._codex_turn = codex_turn
        host._claude_turn = claude_turn
        host._ask = ask
        host._collaborate("change app.py", "codex-model", "high", "claude-model", "ask_every_edit", "codex")
        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        joint = [item[1] for item in host.messages.queue if item[0] == "joint"][-1]
        self.assertEqual(joint.joint_answer, candidate)

    def test_collaboration_rejection_never_prompts_to_apply(self):
        target = self.root / "app.py"
        target.write_text("before", encoding="utf-8")
        candidate = proposal({"path": "app.py", "old_text": "before", "new_text": "after"})
        host = DesktopHost.__new__(DesktopHost)
        host.workspace = self.root
        host.messages = queue.Queue()

        def finished(provider, text):
            turn = TurnRecord(provider, "menu-model", text=text)
            turn.append(Envelope(provider, "native", "turn_finished", {"ok": True}))
            return turn

        host._codex_turn = lambda _text, _model, _effort, _approval, _role: finished("codex", candidate)
        host._claude_turn = lambda _text, _model, _role, _approval: finished("claude", "DISAGREE")
        host._ask = lambda kind, value: value if kind == "handoff" else self.fail("Edit dialog opened before agreement")
        host._collaborate("change app.py", "codex-model", "high", "claude-model", "ask_every_edit", "codex")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        joint = [item[1] for item in host.messages.queue if item[0] == "joint"][-1]
        self.assertIsNone(joint.joint_answer)


if __name__ == "__main__":
    unittest.main()
