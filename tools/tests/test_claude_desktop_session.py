"""Desktop-slice behaviour of the Claude adapter (VLI-159/160), against the fake CLI.

Fable consent, the approval-mode toggle, native session IDs, billing re-checks
and failure events. No test contacts a model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_modes import (  # noqa: E402
    AUTO_ACCEPT_TRUSTED, TrustedFolderStore, claude_auto_accept, confined_edit_target,
)
from claude_runtime import (  # noqa: E402
    ClaudeSession, CreditConsent, Preflight, RuntimeRefused, local_menu,
)
from test_claude_runtime import FakeCliFixture  # noqa: E402


class MenuTests(unittest.TestCase):
    def test_every_offered_model_stays_callable_and_fable_is_flagged(self) -> None:
        menu = local_menu([
            {"value": "default", "resolvedModel": "claude-opus-4-8[1m]", "displayName": "Default (recommended)",
             "supportedEffortLevels": ["low", "max", "bogus"]},
            {"value": "claude-opus-5", "resolvedModel": "claude-opus-5", "displayName": "Opus 5"},
            {"value": "claude-fable-5[1m]", "resolvedModel": "claude-fable-5", "displayName": "Fable 5"},
            {"value": "--dangerous-flag", "displayName": "x"},
            {"value": "has space", "displayName": "x"},
            {"value": "opus%PATH%", "displayName": "x"},
            "not a dict",
        ])
        self.assertEqual([row["value"] for row in menu], ["default", "claude-opus-5", "claude-fable-5[1m]"])
        self.assertEqual(menu[0]["effort_levels"], ["low", "max"])
        self.assertEqual([row["credit_billed"] for row in menu], [False, False, True])
        # The redacted report would print this as "unlisted-claude-opus"; the local picker keeps it callable.
        self.assertEqual(menu[1]["display_name"], "Opus 5")


class ApprovalModeUnitTests(unittest.TestCase):
    def test_confinement_rejects_escapes_and_agent_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as root_name, tempfile.TemporaryDirectory() as outside:
            root = Path(root_name)
            (root / "src").mkdir()
            self.assertEqual(confined_edit_target("src/a.py", root), (root / "src" / "a.py").resolve())
            for bad in ("", None, 3, "a\x00b", "../x", "src/../../x", "src\\..\\..\\x", str(Path(outside) / "x"),
                        ".", "src", ".git/config", ".claude/settings.json", ".CLAUDE/settings.json",
                        ".mcp.json", "sub/.codex/config.toml"):
                with self.subTest(bad=bad):
                    self.assertIsNone(confined_edit_target(bad, root))
            link = root / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks need Developer Mode or admin on Windows")
            self.assertIsNone(confined_edit_target("link/x.txt", root))

    def test_only_file_edit_tools_can_be_auto_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            self.assertIsNone(claude_auto_accept("Bash", {"command": "echo > a"}, root))
            self.assertIsNone(claude_auto_accept("PowerShell", {"command": "Set-Content a b"}, root))
            self.assertIsNone(claude_auto_accept("mcp__fs__write", {"file_path": "a"}, root))
            pinned = claude_auto_accept("NotebookEdit", {"notebook_path": "n.ipynb", "new_source": "x"}, root)
            self.assertEqual(Path(pinned["notebook_path"]), (root / "n.ipynb").resolve())
            self.assertEqual(pinned["new_source"], "x")

    def test_trusted_folder_store_is_explicit_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            store = TrustedFolderStore(root / "state" / "auto.json")
            self.assertFalse(store.is_trusted(root))
            self.assertFalse(store.is_trusted(root / "missing"))
            store.trust(root)
            self.assertTrue(TrustedFolderStore(store.path).is_trusted(root))
            self.assertFalse(store.is_trusted(root / "state"))  # trust is for the exact folder
            store.revoke(root)
            self.assertFalse(store.is_trusted(root))


class DesktopSessionTests(FakeCliFixture):
    """Reuses the fake-CLI fixture (trusted temp workspace, planted billing variables)."""

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def session_argv(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if "argv" in c and "--permission-prompt-tool" in c["argv"]]

    def run_turn(self, session: ClaudeSession, text: str, answer: bool | None = None) -> list:
        session.send(text)
        seen = []
        for envelope in session.events():
            seen.append(envelope)
            if envelope.kind == "approval_request" and envelope.data["policy"] == "ask" and answer is not None:
                session.answer_approval(envelope.data["request_id"], answer)
        return seen

    # -- Fable: explicit per-session confirmation, enforced before spawn -----
    def test_fable_needs_a_confirmation_that_binds_to_one_session(self) -> None:
        with self.assertRaises(RuntimeRefused) as caught:
            ClaudeSession(self.pre, self.root, model="claude-fable-5[1m]", trust=self.trust)
        self.assertEqual(str(caught.exception), "model_may_bill_usage_credits")
        self.assertEqual(self.session_argv(), [])  # refused before any process was spawned
        with self.assertRaises(RuntimeRefused):
            CreditConsent(confirmed_by_user=False)
        consent = CreditConsent(confirmed_by_user=True)
        first = self.open(model="claude-fable-5[1m]", credit_consent=consent)
        self.assertTrue(first.credit_models_allowed)
        with self.assertRaises(RuntimeRefused) as reused:
            self.open(model="claude-fable-5[1m]", credit_consent=consent)
        self.assertEqual(str(reused.exception), "credit_consent_already_used")
        with self.assertRaises(RuntimeRefused):  # a resumed process is a new session: ask again
            self.open(model="claude-fable-5[1m]", resume=first.session_ref)

    def test_switching_to_fable_mid_session_needs_consent(self) -> None:
        session = self.open()
        with self.assertRaises(RuntimeRefused):
            session.set_model("claude-fable-5[1m]")
        session.grant_credit_consent(CreditConsent(confirmed_by_user=True))
        session.set_model("claude-fable-5[1m]")
        self.assertEqual(session.model, "claude-fable-5[1m]")

    def test_default_that_resolves_to_fable_needs_consent_even_if_the_menu_changed(self) -> None:
        # Preflight saw default -> Opus; by the time the chat process starts it is Fable.
        with patch.dict(os.environ, {"FAKE_DEFAULT_MODEL": "claude-fable-5-1"}):
            with self.assertRaises(RuntimeRefused) as caught:
                ClaudeSession(self.pre, self.root, model="default", trust=self.trust)
        self.assertEqual(str(caught.exception), "model_may_bill_usage_credits")
        self.assertNotIn("user_turn", self.log.read_text(encoding="utf-8"))

    def test_default_model_lets_the_cli_choose_and_effort_is_validated(self) -> None:
        self.open(model="default", effort="max")
        argv = self.session_argv()[-1]
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        with self.assertRaises(RuntimeRefused) as caught:
            self.open(model="haiku", effort="max")  # the fake menu offers haiku only "low"
        self.assertEqual(str(caught.exception), "effort_not_offered_for_model")

    # -- billing re-checked inside the chat process ----------------------------
    def test_session_process_is_rechecked_for_a_subscription_login(self) -> None:
        with patch.dict(os.environ, {"FAKE_SESSION_PLAN": "Claude API"}), \
                self.assertRaises(RuntimeRefused) as caught:
            ClaudeSession(self.pre, self.root, model="sonnet", trust=self.trust)
        self.assertEqual(str(caught.exception), "not_a_subscription_plan")
        self.assertNotIn("user_turn", self.log.read_text(encoding="utf-8"))

    def test_api_key_billing_reported_mid_turn_interrupts(self) -> None:
        kinds = [(e.kind, e.data.get("category")) for e in self.run_turn(self.open(), "API KEY INIT")]
        self.assertIn(("runtime_error", "billing"), kinds)
        self.assertTrue(any(c.get("interrupted") for c in self.calls()))

    def test_assistant_errors_become_runtime_errors_without_text(self) -> None:
        seen = self.run_turn(self.open(), "BILLING ERROR")
        error = next(e for e in seen if e.kind == "runtime_error")
        self.assertEqual(error.data, {"category": "billing", "code": "billing_error"})
        self.assertNotIn("PLANTED", json.dumps([e.data for e in seen if e.kind != "text_delta"]))

    # -- native session IDs -----------------------------------------------------
    def test_new_chats_get_a_native_id_up_front_and_resume_keeps_it(self) -> None:
        session = self.open()
        argv = self.session_argv()[-1]
        self.assertEqual(argv[argv.index("--session-id") + 1], session.session_ref)
        resumed = self.open(resume=session.session_ref)
        self.assertEqual(resumed.session_ref, session.session_ref)
        self.assertIn("--resume", self.session_argv()[-1])

    # -- approval toggle (VLI-159) -------------------------------------------
    def test_every_session_routes_edits_and_commands_to_the_host(self) -> None:
        session = self.open()
        settings = [c["settings"] for c in self.calls() if "settings" in c][-1]
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"):
            self.assertIn(tool, settings["permissions"]["ask"])
        argv = self.session_argv()[-1]
        settings_file = Path(argv[argv.index("--settings") + 1])
        self.assertTrue(settings_file.exists())
        session.close()
        self.assertFalse(settings_file.exists())

    def test_ask_every_edit_is_the_default_and_blocks_on_the_user(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        session = self.open()
        self.assertEqual(session.approval_mode, "ask_every_edit")
        seen = self.run_turn(session, f"EDIT {self.root / 'a.txt'}", answer=False)
        request = next(e for e in seen if e.kind == "approval_request")
        self.assertEqual(request.data["policy"], "ask")
        decision = next(e for e in seen if e.kind == "approval_decision")
        self.assertEqual((decision.data["decision"], decision.data["by"]), ("decline", "user"))
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "a")

    def test_auto_accept_needs_a_folder_trusted_for_automatic_edits(self) -> None:
        auto = TrustedFolderStore(self.root / "auto.json")
        with self.assertRaises(RuntimeRefused) as caught:
            self.open(approval_mode=AUTO_ACCEPT_TRUSTED, auto_trust=auto)
        self.assertEqual(str(caught.exception), "folder_not_trusted_for_auto_edits")
        session = self.open()
        with self.assertRaises(RuntimeRefused):
            session.set_approval_mode(AUTO_ACCEPT_TRUSTED, auto)

    def auto_session(self) -> ClaudeSession:
        auto = TrustedFolderStore(self.root / "auto.json")
        auto.trust(self.root)
        return self.open(approval_mode=AUTO_ACCEPT_TRUSTED, auto_trust=auto)

    def test_auto_accept_applies_only_to_in_folder_edits_with_the_path_pinned(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("a", encoding="utf-8")
        seen = self.run_turn(self.auto_session(), "EDIT src/a.txt")
        request = next(e for e in seen if e.kind == "approval_request")
        decision = next(e for e in seen if e.kind == "approval_decision")
        self.assertEqual(request.data["policy"], "auto_accept")
        self.assertEqual(decision.data["by"], "auto_trusted")
        answer = next(c["approval_answer"] for c in self.calls() if "approval_answer" in c)
        self.assertEqual(Path(answer["updatedInput"]["file_path"]), (self.root / "src" / "a.txt").resolve())
        self.assertEqual((self.root / "src" / "a.txt").read_text(encoding="utf-8"), "b")

    def test_auto_accept_still_asks_outside_the_folder_and_for_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            targets = [str(Path(outside) / "x.txt"), "../escape.txt", ".claude/settings.json", ".mcp.json",
                       ".git/config"]
            session = self.auto_session()
            for target in targets:
                with self.subTest(target):
                    seen = self.run_turn(session, f"EDIT {target}", answer=False)
                    request = next(e for e in seen if e.kind == "approval_request")
                    self.assertEqual(request.data["policy"], "ask")
            session.close()
            self.assertFalse((Path(outside) / "x.txt").exists())

    def test_revoking_folder_trust_stops_auto_accept_immediately(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        auto = TrustedFolderStore(self.root / "auto.json")
        auto.trust(self.root)
        session = self.open(approval_mode=AUTO_ACCEPT_TRUSTED, auto_trust=auto)
        auto.revoke(self.root)
        seen = self.run_turn(session, "EDIT a.txt", answer=False)
        self.assertEqual(next(e for e in seen if e.kind == "approval_request").data["policy"], "ask")

    def test_mode_cannot_change_during_a_turn(self) -> None:
        auto = TrustedFolderStore(self.root / "auto.json")
        auto.trust(self.root)
        session = self.open()
        session.send(f"EDIT {self.root / 'a.txt'}")
        events = session.events()
        next(events)  # tool_started: the turn is active
        with self.assertRaises(RuntimeRefused) as caught:
            session.set_approval_mode(AUTO_ACCEPT_TRUSTED, auto)
        self.assertEqual(str(caught.exception), "approval_mode_change_during_turn")
        for envelope in events:
            if envelope.kind == "approval_request":
                session.answer_approval(envelope.data["request_id"], False)
        session.set_approval_mode(AUTO_ACCEPT_TRUSTED, auto)  # allowed between turns

    def test_an_edit_that_never_reached_the_host_is_shown(self) -> None:
        seen = self.run_turn(self.open(), f"UNASKED EDIT {self.root / 'a.txt'}")
        notices = [e for e in seen if e.kind == "notice"]
        self.assertEqual(notices[0].data, {"subtype": "ran_without_host_approval", "tool": "Edit"})

    def test_asked_edits_are_not_flagged_even_without_tool_use_id(self) -> None:
        with patch.dict(os.environ, {"FAKE_TOOL_USE_ID": "omit"}):
            seen = self.run_turn(self.open(), f"EDIT {self.root / 'a.txt'}", answer=True)
        self.assertFalse([e for e in seen if e.kind == "notice"])

    def test_cmd_shim_refuses_arguments_cmd_would_rewrite(self) -> None:
        shim = Preflight([str(self.root / "claude.cmd")], (2, 1, 201), "older_untested", {}, {}, [], [])
        spawned = []
        with self.assertRaises(RuntimeRefused) as caught:
            ClaudeSession(shim, self.root, model="sonnet", session_id="a%PATH%b", trust=self.trust,
                          spawn=lambda *a, **k: spawned.append(a))
        self.assertEqual(str(caught.exception), "unsafe_cmd_argument")
        self.assertEqual(spawned, [])


if __name__ == "__main__":
    unittest.main()
