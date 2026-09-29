"""VLI-160 in the desktop host: recording, curated collaboration handoff, manual handoff. No provider runs."""

from __future__ import annotations

import hashlib
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from tools.conversation import ConversationLog
from tools.desktop import DesktopHost
from tools.desktop_state import TurnRecord
from tools.runtime_events import Envelope

SECRET = "sk-ant-" + "api03-" + "Q" * 24


def finished(provider: str, ref: str, text: str, ok: bool = True) -> TurnRecord:
    turn = TurnRecord(provider, "m")
    turn.append(Envelope(provider, ref, "text_delta", {"text": text}))
    turn.append(Envelope(provider, ref, "turn_finished", {"ok": ok}))
    return turn


class HostConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name) / "proj"
        self.workspace.mkdir()
        host = DesktopHost.__new__(DesktopHost)
        host.messages = queue.Queue()
        host.history = []
        host.workspace = self.workspace
        host.conversation = ConversationLog.open_latest(Path(tmp.name) / "state", self.workspace)
        host.claude_consent = None
        self.host = host

    def on_worker(self, *args) -> None:
        """Collaboration runs on the turn worker thread in the app, never on the Tk thread."""
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.host._collaborate(*args)
            except BaseException as error:  # surfaced to the test below
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(30)
        if errors:
            raise errors[0]

    def test_collaboration_sends_the_candidate_verbatim_and_records_the_curated_packet(self) -> None:
        candidate = "Use Decimal. Config key " + SECRET  # redaction must not alter what is voted on
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        prompts: list[str] = []

        def codex_turn(text, *args):
            turn = finished("codex", "thr_1", candidate if not prompts else f"APPROVE {digest}")
            prompts.append(text)
            self.host._remember(turn, "codex")
            return turn

        def claude_turn(text, *args):
            prompts.append(text)
            turn = finished("claude", "11111111-2222-3333-4444-555555555555", f"APPROVE {digest}")
            self.host._remember(turn, "review")
            return turn

        def ask(kind, data):
            self.assertEqual(kind, "handoff")
            draft, shown_candidate = data
            self.assertEqual(shown_candidate, candidate)
            self.assertNotIn("reply", [item.id for item in draft.items])
            return draft.finalize(draft.render()[0])

        self.host._codex_turn, self.host._claude_turn, self.host._ask = codex_turn, claude_turn, ask
        self.on_worker("Fix rounding", "m", "", "sonnet", "ask_every_edit", "codex")
        review_prompt = prompts[1]
        self.assertIn(f"## Candidate answer (exact text under review)\n{candidate}", review_prompt)
        packet_part = review_prompt.split("## Candidate answer")[0]
        self.assertNotIn(SECRET, packet_part)  # the curated context is redacted
        kinds = [e.kind for e in self.host.conversation.entries]
        self.assertIn("handoff", kinds)
        _, state = self.host.messages.get_nowait()
        self.assertEqual(state.state, "approved")

    def test_cancelled_handoff_leaves_the_candidate_unapproved(self) -> None:
        self.host._codex_turn = lambda *a: finished("codex", "thr_1", "draft")
        self.host._ask = lambda kind, data: None
        self.on_worker("p", "m", "", "sonnet", "ask_every_edit", "codex")
        _, state = self.host.messages.get_nowait()
        self.assertEqual(state.state, "needs_user_decision")
        self.assertNotIn("handoff", [e.kind for e in self.host.conversation.entries])


class TkHandoffDialogTests(unittest.TestCase):
    """Constructs the real window (withdrawn) and drives the dialog's buttons."""

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("no Tk display")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch("tools.desktop.private_state_dir", return_value=Path(tmp.name) / "state"):
            self.host = DesktopHost(self.root)
        self.workspace = Path(tmp.name) / "proj"
        self.workspace.mkdir()
        self.host.workspace = self.workspace
        self.host.conversation = ConversationLog.open_latest(Path(tmp.name) / "state", self.workspace)
        self.host.conversation.record_user("Fix it", mode="gpt_only", providers=["codex"])
        self.host.conversation.record_turn(finished("codex", "thr_1", "Patched with key " + SECRET, ok=False))

    def press(self, label: str, *, uncheck: str | None = None, append: str = "") -> None:
        def act() -> None:
            window = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
            for widget in self._walk(window):
                if uncheck and isinstance(widget, ttk.Checkbutton) and widget.cget("text") == uncheck:
                    widget.invoke()
                if append and isinstance(widget, tk.Text):
                    widget.insert("end", append)
            for widget in self._walk(window):
                if isinstance(widget, ttk.Button) and widget.cget("text") == label:
                    widget.invoke()
                    return
        self.root.after(300, act)

    def _walk(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk(child)

    def test_manual_handoff_prefills_the_other_provider_without_sending(self) -> None:
        self.press("Send reviewed handoff", uncheck="Codex's reply (tail)")
        self.host._handoff_manual()
        packet = self.host.prompt.get("1.0", "end")
        self.assertEqual(self.host.mode.get(), "claude_only")
        self.assertIn("Handoff from Codex to Claude", packet)
        self.assertIn("Reason: the source provider's last turn failed", packet)
        self.assertNotIn("Patched with key", packet)  # the user left the reply out
        self.assertIn("Left out by the user: Codex's reply (tail)", packet)
        self.assertFalse(self.host.busy)  # nothing was sent to a provider
        # Not recorded until it is actually sent (Codex review finding 1).
        self.assertNotEqual(self.host.conversation.entries[-1].kind, "handoff")
        self.assertIsNotNone(self.host.pending_handoff)

    def test_the_handoff_is_scanned_and_recorded_as_actually_sent(self) -> None:
        self.press("Send reviewed handoff")
        self.host._handoff_manual()
        edited = self.host.prompt.get("1.0", "end").strip() + "\nplus " + "glpat-" + "S" * 24
        with patch("tools.desktop.messagebox.askyesno", return_value=False):
            self.assertFalse(self.host._dispatch_pending_handoff(edited, "claude_only"))
        self.assertIsNotNone(self.host.pending_handoff)  # still pending: the next Send checks again
        self.assertNotEqual(self.host.conversation.entries[-1].kind, "handoff")
        clean = self.host.prompt.get("1.0", "end").strip() + "\nOnly look at rounding."
        self.assertTrue(self.host._dispatch_pending_handoff(clean, "claude_only"))
        entry = self.host.conversation.entries[-1]
        self.assertEqual(entry.kind, "handoff")
        self.assertTrue(entry.data["packet"].endswith("Only look at rounding."))  # exactly what was sent
        self.assertEqual(entry.data["redactions"].get("anthropic_key"), 1)  # render counts are kept
        self.assertIsNone(self.host.pending_handoff)

    def test_a_candidate_with_secrets_needs_an_explicit_yes(self) -> None:
        from tools.conversation import draft_handoff
        draft = draft_handoff(self.host.conversation, source="codex", target="claude", user_summary="s")
        candidate = "answer with " + SECRET
        self.press("Send reviewed handoff")
        self.root.after(900, lambda: [w.destroy() for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)])
        with patch("tools.desktop.messagebox.askyesno", return_value=False) as asked:
            self.assertIsNone(self.host._handoff_dialog(draft, candidate))
        self.assertIn("anthropic key", asked.call_args.args[1])
        self.press("Send reviewed handoff")
        with patch("tools.desktop.messagebox.askyesno", return_value=True):
            self.assertIsNotNone(self.host._handoff_dialog(draft, candidate))

    def test_a_secret_pasted_into_the_final_text_is_caught_and_redacted_on_request(self) -> None:
        self.press("Send reviewed handoff", append="\nplus " + "ghp_" + "R" * 36)
        # Declining "send anyway" keeps the dialog open; the test then cancels it.
        self.root.after(900, lambda: [w.destroy() for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)])
        with patch("tools.desktop.messagebox.askyesno", return_value=False) as asked:
            self.host._handoff_manual()
        asked.assert_called_once()
        self.assertIn("github token", asked.call_args.args[1])
        self.assertNotIn("R" * 36, asked.call_args.args[1])  # the warning names the category, not the secret
        self.assertNotEqual(self.host.conversation.entries[-1].kind, "handoff")  # nothing sent or recorded
        self.assertEqual(self.host.prompt.get("1.0", "end").strip(), "")

    def test_conversation_window_opens_and_shows_native_sessions(self) -> None:
        self.host._show_conversation()
        window = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        text = next(w for w in self._walk(window) if isinstance(w, tk.Text)).get("1.0", "end")
        self.assertIn("Codex native sessions (resumable only in Codex): thr_1", text)
        window.destroy()


if __name__ == "__main__":
    unittest.main()
