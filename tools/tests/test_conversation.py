"""VLI-160: persistent shared conversation, curated handoff and redaction. No provider process is used."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.conversation import (
    ConversationLog, HandoffNeedsReview, draft_handoff, redact, workspace_key,
)
from tools.desktop_state import TurnRecord
from tools.runtime_events import Envelope

CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
CODEX_ID = "thr_codex_native_0001"
# Built at runtime so no secret-shaped literal sits in the source.
SECRETS = {
    "anthropic_key": "sk-ant-" + "api03-" + "A" * 24,
    "openai_key": "sk-" + "proj-" + "B" * 30,
    "github_token": "ghp_" + "C" * 36,
    "aws_access_key": "AKIA" + "D" * 16,
    "google_api_key": "AIza" + "E" * 35,
    "slack_token": "xoxb-" + "1234567890-" + "F" * 12,
    "jwt": "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12,
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE" + "x" * 40 + "\n-----END RSA PRIVATE KEY-----",
    # Added after Codex's review of PR #20 (finding 4).
    "stripe_key": "whsec_" + "G" * 32,
    "gitlab_token": "glpat-" + "H" * 20,
    "npm_token": "npm_" + "I" * 36,
    "huggingface_token": "hf_" + "J" * 34,
}


def turn(provider: str, ref: str, text: str, *, ok: bool = True, extra: list[Envelope] | None = None) -> TurnRecord:
    record = TurnRecord(provider, "sonnet" if provider == "claude" else "gpt-6-sol")
    for event in [Envelope(provider, ref, "tool_started", {"tool": "Edit", "tool_use_id": "t1"}),
                  Envelope(provider, ref, "approval_request", {"request_id": "r1", "tool": "Edit", "policy": "ask"}),
                  Envelope(provider, ref, "approval_decision", {"request_id": "r1", "decision": "accept", "by": "user"}),
                  Envelope(provider, ref, "tool_finished", {"tool_use_id": "t1", "is_error": False}),
                  Envelope(provider, ref, "text_delta", {"text": text}),
                  *(extra or []),
                  Envelope(provider, ref, "turn_finished", {"ok": ok, "terminal_reason": None if ok else "limit"})]:
        record.append(event)
    return record


class RedactionTests(unittest.TestCase):
    def test_every_secret_shape_is_replaced_and_only_categories_are_reported(self) -> None:
        text = "\n".join(f"{name}: {value}" for name, value in SECRETS.items())
        text += ("\nDATABASE_PASSWORD=hunter2hunter2\nurl postgres://admin:s3cretpw@db.local/x"
                 "\nAuthorization: Bearer " + "Z" * 30 + "\nmail jerome@example.com"
                 "\npath C:\\Users\\choma\\repo and /home/bob/x")
        clean, counts = redact(text)
        for value in [*SECRETS.values(), "hunter2hunter2", "s3cretpw", "Z" * 30, "jerome@example.com",
                      "choma", "/home/bob"]:
            self.assertNotIn(value, clean)
        for category in [*SECRETS, "secret_assignment", "url_credentials", "bearer_token", "email", "home_path"]:
            self.assertIn(category, counts, category)
        self.assertIn("DATABASE_PASSWORD=[REDACTED:secret_assignment]", clean)
        self.assertIn("C:\\Users\\<user>\\repo", clean)
        self.assertNotIn("hunter2", json.dumps(counts))

    def test_ordinary_code_is_left_alone(self) -> None:
        code = "def total(items):\n    return sum(i.price for i in items)  # see README.md, token count 3\n"
        self.assertEqual(redact(code), (code, {}))


class ConversationLogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.workspace = Path(tmp.name) / "ledger-app"
        self.workspace.mkdir()

    def test_conversation_persists_and_reopens_for_the_workspace(self) -> None:
        log = ConversationLog.open_latest(self.state, self.workspace)
        log.record_user("Fix the ledger", mode="claude_only", providers=["claude"])
        log.record_turn(turn("claude", CLAUDE_ID, "Fixed rounding."))
        reopened = ConversationLog.open_latest(self.state, self.workspace)
        self.assertEqual([e.kind for e in reopened.entries], ["user_message", "turn"])
        turn_entry = reopened.entries[1]
        self.assertEqual(turn_entry.data["approvals"], [{"request_id": "r1", "tool": "Edit", "policy": "ask",
                                                         "decision": "accept", "by": "user"}])
        self.assertEqual(turn_entry.data["tools"][0]["error"], False)
        self.assertNotIn(str(self.workspace), str(log.path))  # the folder name is a hash, not the path
        self.assertEqual(log.path.parent.name, workspace_key(self.workspace))

    def test_native_session_ids_stay_with_their_provider(self) -> None:
        log = ConversationLog.open_latest(self.state, self.workspace)
        log.record_turn(turn("claude", CLAUDE_ID, "a"))
        log.record_turn(turn("codex", CODEX_ID, "b"))
        self.assertEqual(log.native_sessions(), {"claude": [CLAUDE_ID], "codex": [CODEX_ID]})
        with self.assertRaises(ValueError):
            log.append("note", {"x": 1}, session_ref=CODEX_ID)  # an ID must name its issuer
        draft = draft_handoff(log, source="codex", target="claude", user_summary="Continue")
        packet, _ = draft.render()
        self.assertIn(CODEX_ID, packet)
        self.assertNotIn(CLAUDE_ID, packet)  # never offers the target its own or a foreign resume handle
        self.assertIn("not a session transfer", packet)

    def test_a_torn_or_corrupt_line_does_not_lose_the_history(self) -> None:
        log = ConversationLog.open_latest(self.state, self.workspace)
        log.record_user("one", mode="gpt_only", providers=["codex"])
        with open(log.path, "a", encoding="utf-8") as handle:
            handle.write('{"v":1,"seq":2,"kind":"turn"\n')  # crash mid-write
            handle.write('{"v":1,"seq":3,"at":"x","kind":"bogus","data":{}}\n')
        reopened = ConversationLog(log.path, "ledger-app")
        self.assertEqual([e.data["text"] for e in reopened.entries], ["one"])
        self.assertEqual(reopened.damaged_lines, 2)
        self.assertIn("2 damaged line(s)", reopened.render())
        reopened.record_user("two", mode="gpt_only", providers=["codex"])
        self.assertEqual(reopened.entries[-1].seq, 2)

    def test_entries_after_a_torn_line_survive_a_second_reopen(self) -> None:
        log = ConversationLog.open_latest(self.state, self.workspace)
        log.record_user("first", mode="gpt_only", providers=["codex"])
        with open(log.path, "a", encoding="utf-8") as handle:
            handle.write('{"v":1,"seq":2,"kind":"tu')  # torn, no newline (Codex review finding 2)
        again = ConversationLog(log.path, "ledger-app")
        again.record_user("second", mode="gpt_only", providers=["codex"])
        third = ConversationLog(log.path, "ledger-app")
        self.assertEqual([e.data["text"] for e in third.entries], ["first", "second"])
        self.assertEqual(third.damaged_lines, 1)

    def test_a_torn_multibyte_character_costs_one_line_not_the_history(self) -> None:
        log = ConversationLog.open_latest(self.state, self.workspace)
        log.record_user("first — café", mode="gpt_only", providers=["codex"])
        with open(log.path, "ab") as handle:
            handle.write('{"v":1,"text":"'.encode("utf-8") + "—".encode("utf-8")[:1])  # cut mid-character
        again = ConversationLog(log.path, "ledger-app")  # previously raised UnicodeDecodeError
        self.assertEqual(again.damaged_lines, 1)
        again.record_user("second", mode="gpt_only", providers=["codex"])
        third = ConversationLog(log.path, "ledger-app")
        self.assertEqual([e.data["text"] for e in third.entries], ["first — café", "second"])
        self.assertEqual(third.damaged_lines, 1)

    def test_start_new_is_durable_before_the_next_entry(self) -> None:
        old = ConversationLog.open_latest(self.state, self.workspace)
        old.record_user("old", mode="gpt_only", providers=["codex"])
        new = ConversationLog.start_new(self.state, self.workspace)  # Codex review finding 5
        self.assertTrue(new.path.exists())
        self.assertEqual(ConversationLog.open_latest(self.state, self.workspace).path, new.path)

    def test_inspector_render_and_redacted_export(self) -> None:
        log = ConversationLog.open_latest(self.state, self.workspace)
        log.record_user("use key " + SECRETS["anthropic_key"], mode="claude_only", providers=["claude"])
        log.record_turn(turn("claude", CLAUDE_ID, "done"))
        self.assertIn(SECRETS["anthropic_key"], log.render())  # the user's own local record is complete
        exported = log.render(redacted=True)
        self.assertNotIn(SECRETS["anthropic_key"], exported)
        self.assertIn("approval Edit: accept (user)", exported)
        log.forget()
        self.assertFalse(log.path.exists())


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name) / "ledger-app"
        workspace.mkdir()
        self.log = ConversationLog.open_latest(Path(tmp.name) / "state", workspace)
        self.log.record_user("Fix the ledger rounding", mode="gpt_only", providers=["codex"])

    def test_draft_items_are_editable_omittable_and_redacted(self) -> None:
        self.log.record_turn(turn("codex", CODEX_ID, "Patched it. Config uses " + SECRETS["openai_key"]))
        draft = draft_handoff(self.log, source="codex", target="claude", user_summary="Review the patch",
                              selected_files=["src/ledger.py"], file_diffs={"src/ledger.py": "-a\n+b"})
        self.assertEqual([i.id for i in draft.items],
                         ["summary", "goal", "verdict", "tools", "approvals", "reply", "files", "diff:src/ledger.py"])
        self.assertFalse(draft.item("diff:src/ledger.py").included)  # diffs are opt-in
        packet, counts = draft.render()
        self.assertNotIn(SECRETS["openai_key"], packet)
        self.assertEqual(counts, {"openai_key": 1})
        self.assertEqual(draft.finalize(packet)[1], {"openai_key": 1})  # the render's finding is kept
        draft.set_included("reply", False)
        draft.set_included("diff:src/ledger.py", True)
        draft.edit("summary", "Only check the rounding change")
        packet, _ = draft.render()
        self.assertNotIn("Patched it", packet)
        self.assertIn("Left out by the user: Codex's reply (tail)", packet)
        self.assertIn("+b", packet)
        self.assertIn("Only check the rounding change", packet)
        with self.assertRaises(ValueError):
            draft.set_included("summary", False)

    def test_final_edited_text_is_rescanned_before_sending(self) -> None:
        self.log.record_turn(turn("codex", CODEX_ID, "ok"))
        draft = draft_handoff(self.log, source="codex", target="claude", user_summary="go")
        packet, _ = draft.render()
        pasted = packet + "\nAlso: " + SECRETS["github_token"]
        with self.assertRaises(HandoffNeedsReview) as caught:
            draft.finalize(pasted)
        self.assertEqual(caught.exception.counts, {"github_token": 1})
        self.assertNotIn(SECRETS["github_token"], str(caught.exception))
        final, counts = draft.finalize(pasted, send_despite_findings=True)  # the user insisted, knowingly
        self.assertIn(SECRETS["github_token"], final)
        self.assertEqual(counts, {"github_token": 1, "sent_unredacted_by_user": 1})
        entry = draft.record(self.log, final, counts)
        self.assertEqual(entry.data["redactions"]["github_token"], 1)
        self.assertEqual(self.log.entries[-1].kind, "handoff")

    def test_limit_failure_becomes_the_handoff_reason(self) -> None:
        limited = turn("codex", CODEX_ID, "partial", ok=False,
                       extra=[Envelope("codex", CODEX_ID, "rate_limit", {"status": "rejected", "type": "primary"})])
        self.log.record_turn(limited)
        draft = draft_handoff(self.log, source="codex", target="claude", user_summary="Carry on")
        self.assertEqual(draft.reason, "the source provider reached its usage limit")
        self.assertIn("Reason: the source provider reached its usage limit", draft.render()[0])

    def test_handoff_is_bounded_and_says_so(self) -> None:
        self.log.record_turn(turn("codex", CODEX_ID, "x" * 50_000))
        draft = draft_handoff(self.log, source="codex", target="claude", user_summary="s", limit_chars=2_000,
                              file_diffs={"big.py": "+y\n" * 5_000})
        self.assertLessEqual(len(draft.item("reply").text), 1_000)  # the reply tail is trimmed up front
        draft.set_included("diff:big.py", True)
        packet, counts = draft.render()
        self.assertLessEqual(len(packet), 2_000)
        self.assertIn("Handoff shortened", packet)
        self.assertEqual(counts.get("shortened"), 1)

    def test_same_provider_handoff_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            draft_handoff(self.log, source="claude", target="claude", user_summary="s")


if __name__ == "__main__":
    unittest.main()
