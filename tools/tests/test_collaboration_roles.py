"""Agent role agreement and final-review ordering without provider calls."""

import hashlib
import json
from pathlib import Path
import queue
import tempfile
import unittest

from tools.conversation import ConversationLog
from tools.desktop import DesktopHost
from tools.desktop_state import RolePlan, TurnRecord, explicit_approval, parse_role_plan
from tools.runtime_events import Envelope


def finished(provider, text):
    turn = TurnRecord(provider, "menu-model", text=text)
    turn.append(Envelope(provider, "native", "turn_finished", {"ok": True}))
    return turn


class RolePlanTests(unittest.TestCase):
    def test_explicit_hash_vote_allows_a_reason_but_rejects_ambiguity(self):
        digest = hashlib.sha256(b"candidate").hexdigest()
        self.assertTrue(explicit_approval(f"APPROVE {digest}", digest))
        self.assertTrue(explicit_approval(f"APPROVE {digest}\n\nThe answer is correct.", digest))
        wrong = digest[:-1] + ("0" if digest[-1] != "0" else "1")
        for output in (f"I think APPROVE {digest}", f"APPROVE {wrong}",
                       f"APPROVE {digest}\nDISAGREE after reconsidering",
                       f"APPROVE {digest}\nCHANGES REQUESTED",
                       f"APPROVE {digest}\nA different candidate hash: {wrong}",
                       f"APPROVE {digest}\nAPPROVE another version",
                       f"DISAGREE\nAPPROVE {digest}",
                       f"Here is my answer.\nAPPROVE {digest}",
                       "The answer is correct.", f"APPROVE {digest}\n" + "x" * 2048):
            with self.subTest(output=output[:30]):
                self.assertFalse(explicit_approval(output, digest))

    def test_exact_role_plan_and_rejections(self):
        expected = RolePlan("codex", "claude", "code first")
        self.assertEqual(parse_role_plan('{"lead":"codex","final":"claude","reason":"code first"}'), expected)
        self.assertEqual(parse_role_plan('```json\n{"lead":"codex","final":"claude","reason":"code first"}\n```'), expected)
        for value in ('{"lead":"codex","final":"other","reason":"x"}',
                      '{"lead":"codex","final":"claude","reason":"x","approved":true}',
                      'explanation {"lead":"codex","final":"claude","reason":"x"}',
                      '{"lead":"codex","final":"claude","reason":""}',
                      '{"lead":{},"final":"claude","reason":"x"}',
                      '{"lead":"codex","lead":"claude","final":"claude","reason":"x"}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_role_plan(value)

    def make_host(self, first_plan, second_plan, role_choice=None, *, final_vote=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        host = DesktopHost.__new__(DesktopHost)
        host.workspace = Path(temp.name)
        host.messages = queue.Queue()
        host.conversation = ConversationLog(Path(temp.name) / "conversation.jsonl", "ws")  # VLI-160
        calls = []
        digest = hashlib.sha256(b"candidate").hexdigest()

        def turn(provider, _text, _model, _effort_or_role, *rest):
            # Codex and Claude adapters have different arguments; inspect the
            # role from their final positional argument.
            role = rest[-1] if provider == "codex" else _effort_or_role
            calls.append((provider, role))
            if role.startswith("planning"):
                value = first_plan if provider == "codex" else second_plan
            elif role.startswith("draft"):
                value = "candidate"
            else:
                value = f"APPROVE {digest}" if final_vote else "DISAGREE"
            return finished(provider, value)

        host._codex_turn = lambda text, model, effort, approval, role: turn("codex", text, model, effort, approval, role)
        host._claude_turn = lambda text, model, role, approval: turn("claude", text, model, role, approval)
        def ask(kind, value):
            if kind == "handoff":  # VLI-160: a curated draft; accept it as rendered
                draft, _candidate = value
                return draft.finalize(draft.render()[0])
            if kind == "role_decision":
                return role_choice
            raise AssertionError("Unexpected dialog: " + kind)
        host._ask = ask
        return host, calls

    def test_agreed_roles_make_other_provider_final_reviewer(self):
        plan = json.dumps({"lead": "codex", "final": "claude", "reason": "Codex drafts code"})
        host, calls = self.make_host(plan, plan)
        host._collaborate("Build it", "codex-model", "high", "claude-model", "ask_every_edit")
        self.assertEqual([p for p, _ in calls], ["codex", "claude", "codex", "codex", "claude"])
        joint = [item for item in list(host.messages.queue) if item[0] == "joint"][-1][1]
        self.assertEqual(joint.joint_answer, "candidate")
        self.assertEqual(joint.final_provider, "claude")

    def test_agreed_lead_as_final_reviewer_votes_last(self):
        plan = json.dumps({"lead": "codex", "final": "codex", "reason": "Codex should sign off"})
        host, calls = self.make_host(plan, plan)
        host._collaborate("Build it", "codex-model", "high", "claude-model", "ask_every_edit")
        self.assertEqual([p for p, _ in calls], ["codex", "claude", "codex", "claude", "codex"])
        joint = [item for item in list(host.messages.queue) if item[0] == "joint"][-1][1]
        self.assertEqual(joint.final_provider, "codex")
        self.assertEqual(joint.joint_answer, "candidate")

    def test_disagreement_requires_user_choice_before_draft(self):
        codex = json.dumps({"lead": "codex", "final": "codex", "reason": "code"})
        claude = json.dumps({"lead": "claude", "final": "claude", "reason": "writing"})
        host, calls = self.make_host(codex, claude, role_choice=None)
        host._collaborate("Task", "model", "high", "model", "ask_every_edit")
        self.assertEqual(len(calls), 2)
        joint = [item for item in list(host.messages.queue) if item[0] == "joint"][-1][1]
        self.assertIsNone(joint.joint_answer)

    def test_user_can_choose_valid_plan_but_two_votes_are_still_required(self):
        codex = json.dumps({"lead": "codex", "final": "codex", "reason": "code"})
        claude = json.dumps({"lead": "claude", "final": "claude", "reason": "writing"})
        host, calls = self.make_host(codex, claude, role_choice="claude", final_vote=False)
        host._collaborate("Task", "model", "high", "model", "ask_every_edit")
        self.assertEqual([p for p, _ in calls], ["codex", "claude", "claude", "codex"])
        joint = [item for item in list(host.messages.queue) if item[0] == "joint"][-1][1]
        self.assertIsNone(joint.joint_answer)

    def test_invalid_plan_does_not_become_an_automatic_lead(self):
        claude = json.dumps({"lead": "claude", "final": "codex", "reason": "use both strengths"})
        host, calls = self.make_host("invalid output", claude, role_choice=None)
        host._collaborate("Task", "model", "high", "model", "ask_every_edit")
        self.assertEqual(len(calls), 2)
        joint = [item for item in list(host.messages.queue) if item[0] == "joint"][-1][1]
        self.assertIsNone(joint.joint_answer)


if __name__ == "__main__":
    unittest.main()
