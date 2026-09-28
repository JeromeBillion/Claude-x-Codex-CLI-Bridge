"""Windows-first personal desktop timeline. Run: py -3 -m tools.desktop.

The installed provider CLIs own auth and execution. This host uses no API keys.
The existing bridge.ps1 / tools.agent_bridge CLI remains independent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from tools.claude_runtime import CREDIT_BILLED, ClaudeSession, Preflight, TrustStore, preflight
from tools.codex_app_server import AppServerTransport, CodexRuntime, ThreadStore, TrustedFolderStore
from tools.codex_probe import safe_child_env
from tools.desktop_state import BLOCKING_KINDS, Collaboration, TurnRecord, handoff_text
from tools.runtime_events import Envelope


def private_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    return Path(base) / "ClaudeCodexDesktop" if base else Path.home() / ".claude-codex-desktop"


class DesktopHost:
    """Serialized workspace turns; all UI callbacks run on the Tk main thread."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Claude x Codex Desktop")
        self.root.geometry("1050x760")
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.workspace: Path | None = None
        self.codex: CodexRuntime | None = None
        self.codex_transport: AppServerTransport | None = None
        self.claude_preflight: Preflight | None = None
        self.claude_session: ClaudeSession | None = None
        self.claude_model: str | None = None
        self.fable_confirmed = False
        self.history: list[TurnRecord] = []
        state = private_state_dir()
        self.codex_threads = ThreadStore(state / "codex-threads.json")
        self.codex_trust = TrustedFolderStore(state / "codex-trusted.json")
        self.claude_trust = TrustStore(state / "claude-trusted.json")

        self.mode = tk.StringVar(value="gpt_only")
        self.approval = tk.StringVar(value="ask_every_edit")
        self.codex_model = tk.StringVar()
        self.codex_effort = tk.StringVar()
        self.claude_choice = tk.StringVar()
        self.collaboration_lead = tk.StringVar(value="codex")
        self.status = tk.StringVar(value="Select a local project. No model turn starts on Connect.")
        self._build()
        self.root.after(50, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Choose project", command=self._choose).grid(row=0, column=0)
        self.project_label = ttk.Label(top, text="No project selected")
        self.project_label.grid(row=0, column=1, sticky="w", padx=10)
        ttk.Button(top, text="Connect / refresh catalogs", command=self._connect).grid(row=0, column=2)
        top.columnconfigure(1, weight=1)

        options = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        options.pack(fill="x")
        for column, (value, label) in enumerate((("gpt_only", "GPT only"),
                                                  ("collaboration", "Collaboration"),
                                                  ("claude_only", "Claude only"))):
            ttk.Radiobutton(options, text=label, variable=self.mode, value=value).grid(row=0, column=column, sticky="w")
        ttk.Label(options, text="Codex model").grid(row=1, column=0, sticky="w")
        self.codex_picker = ttk.Combobox(options, textvariable=self.codex_model, state="readonly", width=28)
        self.codex_picker.grid(row=2, column=0, sticky="w")
        ttk.Label(options, text="Effort").grid(row=1, column=1, sticky="w")
        self.effort_picker = ttk.Combobox(options, textvariable=self.codex_effort, state="readonly", width=16)
        self.effort_picker.grid(row=2, column=1, sticky="w")
        self.codex_picker.bind("<<ComboboxSelected>>", lambda _: self._efforts())
        ttk.Label(options, text="Claude model").grid(row=1, column=2, sticky="w")
        self.claude_picker = ttk.Combobox(options, textvariable=self.claude_choice, state="readonly", width=28)
        self.claude_picker.grid(row=2, column=2, sticky="w")
        ttk.Label(options, text="Edit approval").grid(row=1, column=3, sticky="w")
        ttk.Combobox(options, textvariable=self.approval, state="readonly", width=27,
                     values=("ask_every_edit", "auto_accept_trusted")).grid(row=2, column=3, sticky="w")
        ttk.Label(options, text="Collaboration lead").grid(row=1, column=4, sticky="w")
        ttk.Combobox(options, textvariable=self.collaboration_lead, state="readonly", width=12,
                     values=("codex", "claude")).grid(row=2, column=4, sticky="w")
        ttk.Label(options, text="Ask every edit = read-only proposal until per-edit apply is available.").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=4)
        ttk.Label(options, text="Auto-accept requires explicit trust of the exact folder; network remains disabled for Codex.").grid(
            row=4, column=0, columnspan=5, sticky="w")

        self.timeline = tk.Text(self.root, wrap="word", state="disabled", height=25)
        self.timeline.pack(fill="both", expand=True, padx=10)
        composer = ttk.Frame(self.root, padding=10)
        composer.pack(fill="x")
        self.prompt = tk.Text(composer, height=4, wrap="word")
        self.prompt.pack(side="left", fill="x", expand=True)
        buttons = ttk.Frame(composer)
        buttons.pack(side="right", padx=8)
        self.send_button = ttk.Button(buttons, text="Send turn", command=self._send)
        self.send_button.pack(fill="x")
        ttk.Button(buttons, text="Interrupt", command=self._interrupt).pack(fill="x")
        ttk.Label(self.root, textvariable=self.status, padding=(10, 0, 10, 8)).pack(anchor="w")

    def _line(self, value: str) -> None:
        self.timeline.configure(state="normal")
        self.timeline.insert("end", value)
        self.timeline.see("end")
        self.timeline.configure(state="disabled")

    def _choose(self) -> None:
        if self.busy:
            return
        selected = filedialog.askdirectory(title="Choose local coding project", mustexist=True)
        if not selected:
            return
        workspace = Path(selected).resolve(strict=True)
        if self.workspace != workspace:
            self._disconnect()
            self.history.clear()
            self.fable_confirmed = False
        self.workspace = workspace
        self.project_label.configure(text=str(workspace))

    def _work(self, action: Callable[[], None]) -> None:
        if self.busy:
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        def run() -> None:
            try:
                action()
            except Exception as exc:
                # Error messages may contain local paths or account details; show only type.
                self.messages.put(("error", type(exc).__name__))
            finally:
                self.messages.put(("idle", None))
        threading.Thread(target=run, daemon=True).start()

    def _connect(self) -> None:
        if self.workspace is None:
            messagebox.showinfo("Project needed", "Choose a local project first.")
            return
        workspace = self.workspace
        mode = self.mode.get()
        self.status.set("Checking installed CLI and account. No model turn is being sent.")
        def connect() -> None:
            if mode != "claude_only":
                if self.codex is None:
                    transport = AppServerTransport(env=safe_child_env())
                    try:
                        runtime = CodexRuntime(transport, self.codex_threads)
                        runtime.initialize()
                        self.codex, self.codex_transport = runtime, transport
                    except Exception:
                        transport.close()
                        raise
                self.messages.put(("codex_catalog", self.codex.discover()))
            if mode != "gpt_only":
                self.claude_preflight = preflight()
                self.messages.put(("claude_catalog", self.claude_preflight.models))
            self.messages.put(("status", "CLI checks complete. Catalog presence does not prove model entitlement."))
        self._work(connect)

    def _efforts(self) -> None:
        model = self.codex_model.get()
        row = self.codex.models.get(model, {}) if self.codex else {}
        if row.get("hidden"):
            self.status.set("Selected model is marked hidden by the installed Codex CLI; callable access is unverified.")
        efforts = [entry.get("reasoningEffort") for entry in row.get("supportedReasoningEfforts", [])]
        efforts = [value for value in efforts if isinstance(value, str)]
        self.effort_picker.configure(values=efforts)
        self.codex_effort.set(row.get("defaultReasoningEffort") if row.get("defaultReasoningEffort") in efforts else (efforts[0] if efforts else ""))

    def _ask(self, kind: str, data: Any) -> Any:
        signal = threading.Event()
        answer: dict[str, Any] = {}
        self.messages.put(("dialog", (kind, data, signal, answer)))
        signal.wait()
        return answer.get("value")

    def _send(self) -> None:
        if self.workspace is None:
            messagebox.showinfo("Project needed", "Choose a local project first.")
            return
        prompt = self.prompt.get("1.0", "end").strip()
        if not prompt:
            return
        mode, approval = self.mode.get(), self.approval.get()
        if mode != "claude_only" and self.codex is None or mode != "gpt_only" and self.claude_preflight is None:
            messagebox.showinfo("Connect needed", "Connect the selected provider CLI first.")
            return
        if mode != "claude_only" and not self.codex_model.get() or mode != "gpt_only" and not self.claude_choice.get():
            messagebox.showinfo("Model needed", "Choose a model from each selected provider's live catalog.")
            return
        if approval == "auto_accept_trusted" and mode != "gpt_only":
            messagebox.showwarning("Claude approval mode pending",
                                   "Claude auto-accept is not wired into this host yet. Choose ask_every_edit for Claude or collaboration.")
            return
        if approval == "auto_accept_trusted":
            codex_needs_trust = mode != "claude_only" and not self.codex_trust.is_trusted(self.workspace)
            claude_needs_trust = mode != "gpt_only" and not self.claude_trust.is_trusted(self.workspace)
            if codex_needs_trust or claude_needs_trust:
                if not messagebox.askyesno("Trust this exact folder?", f"Allow automatic edits in:\n{self.workspace}\n\nProject hooks and MCP servers may run."):
                    return
                if codex_needs_trust:
                    self.codex_trust.trust(self.workspace)
                if claude_needs_trust:
                    self.claude_trust.trust(self.workspace)
        elif mode != "gpt_only" and not self.claude_trust.is_trusted(self.workspace):
            if not messagebox.askyesno("Trust this folder for Claude?", f"Claude Code headless sessions may run project hooks and MCP servers in:\n{self.workspace}"):
                return
            self.claude_trust.trust(self.workspace)
        selected_claude = self.claude_choice.get()
        if mode != "gpt_only" and CREDIT_BILLED.search(selected_claude) and not self.fable_confirmed:
            if not messagebox.askyesno("Fable credits", "Fable may consume usage credits in a headless session. Allow Fable for this app session?"):
                return
            self.fable_confirmed = True
        self.prompt.delete("1.0", "end")
        self._line(f"\nUSER ({mode}): {prompt}\n")
        model, effort, lead = self.codex_model.get(), self.codex_effort.get(), self.collaboration_lead.get()
        self._work(lambda: self._run_turn(mode, approval, prompt, model, effort, selected_claude, lead))

    def _run_turn(self, mode: str, approval: str, prompt: str, model: str,
                  effort: str, claude_model: str, lead: str) -> None:
        if mode == "gpt_only":
            turn = self._codex_turn(prompt, model, effort, approval, "answer")
            self.messages.put(("verdict", (turn, "Codex")))
        elif mode == "claude_only":
            turn = self._claude_turn(prompt, claude_model, "answer")
            self.messages.put(("verdict", (turn, "Claude")))
        else:
            self._collaborate(prompt, model, effort, claude_model, approval, lead)

    def _codex_turn(self, text: str, model: str, effort: str, approval: str, role: str) -> TurnRecord:
        assert self.codex and self.workspace
        runtime = self.codex
        if runtime.thread_id is None or runtime.workspace != self.workspace or runtime.approval_mode != approval:
            runtime.open_thread(self.workspace, approval_mode=approval, trusted_folders=self.codex_trust)
        turn = TurnRecord("codex", model)
        self.messages.put(("line", f"\nCODEX {role.upper()} / {model} / native thread {runtime.thread_id}\n"))
        runtime.start_turn(text, model, effort or None)
        while True:
            try:
                event = runtime.next_event(timeout=600)
            except queue.Empty:
                if runtime.active_turn_id:
                    try:
                        runtime.interrupt()
                    except Exception:
                        pass
                event = Envelope("codex", runtime.thread_id, "turn_finished",
                                 {"ok": False, "terminal_reason": "host_timeout"})
            turn.append(event)
            self.messages.put(("event", event))
            if event.kind in BLOCKING_KINDS:
                if event.kind == "approval_request" and event.data.get("family") in {"command", "file_change"}:
                    decision = self._ask("codex_approval", (event, approval))
                    runtime.decide_approval(event.data["request_id"], decision or "decline")
                else:
                    self._ask("unsupported_approval", event)
                    runtime.interrupt()
            if turn.finished:
                if event.kind in {"runtime_error", "process_exited"} and runtime.active_turn_id:
                    try:
                        runtime.interrupt()
                    except Exception:
                        pass
                self.history.append(turn)
                return turn

    def _claude_turn(self, text: str, model: str, role: str) -> TurnRecord:
        assert self.claude_preflight and self.workspace
        if self.claude_session is None or self.claude_model != model:
            previous = self.claude_session
            native = previous.session_ref if previous else None
            if previous:
                previous.close()
            self.claude_session = ClaudeSession(
                self.claude_preflight, self.workspace, model=model,
                resume=native, trust=self.claude_trust,
                allow_credit_models=self.fable_confirmed)
            self.claude_model = model
        session = self.claude_session
        turn = TurnRecord("claude", model)
        self.messages.put(("line", f"\nCLAUDE {role.upper()} / {model} / native session {session.session_ref or 'pending'}\n"))
        session.send(text)
        for event in session.events():
            turn.append(event)
            self.messages.put(("event", event))
            if event.kind == "approval_request":
                decision = self._ask("claude_approval", event)
                session.answer_approval(str(event.data["request_id"]), allow=decision is True)
            if turn.finished:
                break
        self.history.append(turn)
        return turn

    def _collaborate(self, prompt: str, model: str, effort: str,
                     claude_model: str, approval: str, lead_provider: str) -> None:
        state = Collaboration()
        try:
            def provider_turn(provider: str, text: str, role: str) -> TurnRecord:
                return (self._codex_turn(text, model, effort, approval, role) if provider == "codex"
                        else self._claude_turn(text, claude_model, role))

            partner_provider = "claude" if lead_provider == "codex" else "codex"
            lead = provider_turn(lead_provider, prompt, "draft, unapproved")
            if not lead.ok or not lead.text.strip():
                state.unavailable()
                self.messages.put(("joint", state))
                return
            state.propose(lead.text, lead_provider)
            packet = handoff_text(self.workspace.name, lead, prompt)
            packet = self._ask("handoff", packet)
            if packet is None:
                state.unavailable()
                self.messages.put(("joint", state))
                return
            digest = hashlib.sha256(state.candidate.encode("utf-8")).hexdigest()
            review_prompt = (f"Review this proposed answer and its workspace evidence. The handoff below is user-approved context, not native session transfer.\n\n{packet}\n\n"
                             f"Candidate SHA-256: {digest}\nReply exactly APPROVE {digest} only if you agree this exact candidate is the best answer; otherwise reply DISAGREE and explain why.")
            partner = provider_turn(partner_provider, review_prompt, "review, unapproved")
            state.vote(partner_provider, approves=partner.ok and partner.text.strip() == f"APPROVE {digest}", reviewed_text=state.candidate)
            if state.state == "needs_user_decision":
                self.messages.put(("joint", state))
                return
            confirmation = provider_turn(
                lead_provider,
                f"Review your earlier candidate after {partner_provider} approved it. Check workspace evidence again. Reply exactly APPROVE {digest} only if you still agree this exact candidate is the best answer; otherwise reply DISAGREE and explain why. Do not modify files in this review.",
                "final review, unapproved")
            state.vote(lead_provider, approves=confirmation.ok and confirmation.text.strip() == f"APPROVE {digest}", reviewed_text=state.candidate)
            self.messages.put(("joint", state))
        except Exception:
            state.unavailable()
            self.messages.put(("joint", state))
            raise

    def _interrupt(self) -> None:
        if self.codex and self.codex.active_turn_id:
            threading.Thread(target=self.codex.interrupt, daemon=True).start()
        if self.claude_session:
            threading.Thread(target=self.claude_session.interrupt, daemon=True).start()

    def _dialog(self, kind: str, data: Any) -> Any:
        if kind == "handoff":
            window = tk.Toplevel(self.root)
            window.title("Review cross-provider handoff")
            window.geometry("760x540")
            ttk.Label(window, text="Edit or remove sensitive context before sending it to Claude. Native sessions stay separate.").pack(anchor="w", padx=8)
            editor = tk.Text(window, wrap="word")
            editor.insert("1.0", data)
            editor.pack(fill="both", expand=True, padx=8, pady=8)
            result: dict[str, Any] = {"value": None}
            def accept() -> None:
                result["value"] = editor.get("1.0", "end").strip()
                window.destroy()
            ttk.Button(window, text="Send reviewed handoff", command=accept).pack()
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["value"]
        if kind == "unsupported_approval":
            messagebox.showwarning("Unsupported approval", "This request needs a dedicated consent form. The turn will be interrupted.")
            return None
        if kind == "resolution":
            window = tk.Toplevel(self.root)
            window.title("Collaboration needs your decision")
            window.geometry("620x270")
            ttk.Label(window, text="The providers did not jointly approve the result. Review the drafts in the timeline.",
                      wraplength=590).pack(anchor="w", padx=12, pady=12)
            result: dict[str, Any] = {"value": "leave_pending"}
            def choose(value: str) -> None:
                result["value"] = value
                window.destroy()
            ttk.Button(window, text=f"Use {data.source_provider.title()} draft as my decision", state="normal" if data.candidate else "disabled",
                       command=lambda: choose("use_draft")).pack(fill="x", padx=12, pady=4)
            ttk.Button(window, text="Leave pending and write a new instruction",
                       command=lambda: choose("leave_pending")).pack(fill="x", padx=12, pady=4)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["value"]
        event: Envelope = data[0] if kind == "codex_approval" else data
        detail = event.data.get("command") or event.data.get("input") or event.data.get("reason") or event.data.get("family")
        if kind == "codex_approval":
            if data[1] == "ask_every_edit":
                messagebox.showwarning("Read-only proposal mode",
                                       f"Codex requested permission beyond read-only execution:\n\n{detail}\n\nThis host cannot stage and approve each edit yet. The request will be declined.")
                return "decline"
            return "accept" if messagebox.askyesno("Codex approval required", f"Review and allow this one request?\n\n{detail}") else "decline"
        return messagebox.askyesno("Claude approval required", f"Review and allow this one tool request?\n\n{detail}")

    def _drain(self) -> None:
        try:
            while True:
                kind, data = self.messages.get_nowait()
                if kind == "dialog":
                    name, payload, signal, answer = data
                    try:
                        answer["value"] = self._dialog(name, payload)
                    finally:
                        signal.set()
                elif kind == "line":
                    self._line(data)
                elif kind == "event":
                    event: Envelope = data
                    if event.kind == "text_delta":
                        self._line(str(event.data.get("text", "")))
                    elif event.kind in {"rate_limit", "runtime_error", "process_exited", "notice", "tool_started", "tool_finished"}:
                        self._line(f"\n[{event.provider} {event.kind}: {event.data}]\n")
                elif kind == "codex_catalog":
                    models = list(self.codex.models) if self.codex else []
                    self.codex_picker.configure(values=models)
                    if models:
                        self.codex_model.set(models[0])
                        self._efforts()
                    self._line(f"\nCodex connected. {len(models)} catalog models; plan category: {data.get('planCategory')}.\n")
                    limits = (data.get("rateLimits") or {}).get("rateLimits") or {}
                    primary = (limits.get("primary") or {}).get("usedPercent")
                    if isinstance(primary, (int, float)) and 0 <= primary <= 100:
                        self._line(f"Codex primary allowance used: {primary:.0f}%.\n")
                elif kind == "claude_catalog":
                    models = [row["value"] for row in data if isinstance(row.get("value"), str)]
                    self.claude_picker.configure(values=models)
                    if models:
                        self.claude_choice.set(models[0])
                    self._line(f"\nClaude connected. {len(models)} catalog choices.\n")
                elif kind == "verdict":
                    turn, owner = data
                    self._line(f"\n[{owner} {'answer complete' if turn.ok else 'failed or incomplete: ' + str(turn.failure)}]\n")
                elif kind == "joint":
                    state: Collaboration = data
                    if state.joint_answer is not None:
                        self._line(f"\n[Jointly approved by Claude and Codex]\n{state.joint_answer}\n")
                    else:
                        self._line("\n[Collaboration unresolved. Drafts and reviews above are unapproved. Choose the next action.]\n")
                        if self._dialog("resolution", state) == "use_draft":
                            self._line(f"\n[User selected {state.source_provider} draft; not jointly approved]\n{state.candidate}\n")
                elif kind == "error":
                    self._line(f"\n[Host failure: {data}. Turn unapproved.]\n")
                    self.status.set("Failed. See timeline; no approval was inferred.")
                elif kind == "status":
                    self.status.set(data)
                elif kind == "idle":
                    self.busy = False
                    self.send_button.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _disconnect(self) -> None:
        if self.codex_transport:
            self.codex_transport.close()
        if self.claude_session:
            self.claude_session.close()
        self.codex = self.codex_transport = self.claude_session = None
        self.claude_preflight = None
        self.claude_model = None

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno("Active turn", "A turn is active. Interrupt and close?"):
            return
        self._interrupt()
        self._disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    DesktopHost(root)
    root.mainloop()


if __name__ == "__main__":
    main()
