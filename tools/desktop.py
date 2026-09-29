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

from tools.claude_runtime import (ClaudeSession, CreditConsent, Preflight, RuntimeRefused, TrustStore,
                                  is_credit_billed, preflight)
from tools.capability_inventory import CapabilityRow, claude_capabilities, codex_capabilities
from tools.codex_app_server import AppServerTransport, CodexRuntime, ThreadStore, TrustedFolderStore
from tools.codex_probe import safe_child_env
from tools.conversation import (ConversationLog, HandoffDraft, HandoffNeedsReview, describe_redactions,
                                draft_handoff, redact)
from tools.desktop_state import BLOCKING_KINDS, Collaboration, RolePlan, TurnRecord, parse_role_plan
from tools.runtime_events import Envelope
from tools.staged_edits import EditProposalError, StagedEdit, is_agent_config_path, stage_proposals


def private_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    return Path(base) / "ClaudeCodexDesktop" if base else Path.home() / ".claude-codex-desktop"


class DesktopHost:
    """Serialized workspace turns; all UI callbacks run on the Tk main thread."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._ui_thread = threading.current_thread()  # the only thread that may open dialogs directly
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
        # The user's yes for credit-billed Claude models, consumed by the next new Claude session.
        self.claude_consent: CreditConsent | None = None
        self.history: list[TurnRecord] = []
        # The one persistent, inspectable record of this workspace's conversation (VLI-160).
        self.conversation: ConversationLog | None = None
        # A manual handoff waiting in the prompt box; recorded only when it is actually sent.
        self.pending_handoff: HandoffDraft | None = None
        state = private_state_dir()
        self.codex_threads = ThreadStore(state / "codex-threads.json")
        # One list of folders trusted for automatic edits, shared by both providers.
        self.codex_trust = TrustedFolderStore(state / "codex-trusted.json")
        self.claude_trust = TrustStore(state / "claude-trusted.json")

        self.mode = tk.StringVar(value="gpt_only")
        self.approval = tk.StringVar(value="ask_every_edit")
        self.codex_model = tk.StringVar()
        self.codex_effort = tk.StringVar()
        self.claude_choice = tk.StringVar()
        self.status = tk.StringVar(value="Select a local project. No model turn starts on Connect.")
        self._build()
        self._drain_job = self.root.after(50, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Choose project", command=self._choose).grid(row=0, column=0)
        self.project_label = ttk.Label(top, text="No project selected")
        self.project_label.grid(row=0, column=1, sticky="w", padx=10)
        ttk.Button(top, text="Connect / refresh catalogs", command=self._connect).grid(row=0, column=2)
        ttk.Button(top, text="Inspect capabilities", command=self._inspect_capabilities).grid(row=0, column=3, padx=6)
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
        ttk.Label(options, text="In collaboration, both providers nominate the lead and final reviewer.",
                  wraplength=260).grid(row=1, column=4, rowspan=2, sticky="w")
        ttk.Label(options, text="Ask every edit = read-only Codex turn, then inspect and approve each proposed file diff.").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=4)
        ttk.Label(options, text="Auto-accept requires explicit trust of the exact folder; network remains disabled for Codex.").grid(
            row=4, column=0, columnspan=5, sticky="w")
        ttk.Label(options, text="Collaboration uses up to five provider turns: two role plans, a draft and two reviews.").grid(
            row=5, column=0, columnspan=5, sticky="w")

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
        ttk.Button(buttons, text="Hand off...", command=self._handoff_manual).pack(fill="x")
        ttk.Button(buttons, text="Conversation", command=self._show_conversation).pack(fill="x")
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
            self.claude_consent = None
        self.workspace = workspace
        self.project_label.configure(text=str(workspace))
        self.conversation = ConversationLog.open_latest(private_state_dir(), workspace)
        if self.conversation.entries:
            sessions = self.conversation.native_sessions()
            self._line(f"\nReopened this project's conversation: {len(self.conversation.entries)} entries "
                       f"(Claude sessions {len(sessions['claude'])}, Codex threads {len(sessions['codex'])}). "
                       "Use Conversation to read it.\n")

    def _work(self, action: Callable[[], None]) -> None:
        if self.busy:
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        def run() -> None:
            try:
                action()
            except RuntimeRefused as exc:
                # Adapter refusals carry a fixed reason code (e.g. cli_too_old), never private text.
                self.messages.put(("error", f"Claude refused: {exc}"))
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

    def _inspect_capabilities(self) -> None:
        if self.workspace is None:
            messagebox.showinfo("Project needed", "Choose a local project first.")
            return
        mode = self.mode.get()
        if mode != "claude_only" and self.codex is None or mode != "gpt_only" and self.claude_preflight is None:
            messagebox.showinfo("Connect needed", "Connect the selected provider CLI first.")
            return
        self.status.set("Reading installed CLI capabilities and MCP health. No model turn is being sent.")
        def inspect() -> None:
            rows: list[CapabilityRow] = []
            if mode != "claude_only":
                rows.extend(codex_capabilities(self.codex, self.workspace))
            if mode != "gpt_only":
                rows.extend(claude_capabilities(self.claude_preflight.executable))
            self.messages.put(("capabilities", rows))
        self._work(inspect)

    def _show_capabilities(self, rows: list[CapabilityRow]) -> None:
        window = tk.Toplevel(self.root)
        window.title("Installed CLI capability inventory")
        window.geometry("900x620")
        ttk.Label(window, text="Reported by the installed CLIs. A listing or health check does not prove a tool call works. "
                  "Provider approvals still apply.", wraplength=860).pack(anchor="w", padx=8, pady=8)
        body = ttk.Frame(window, padding=(8, 0, 8, 8))
        body.pack(fill="both", expand=True)
        viewer = tk.Text(body, wrap="word")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=viewer.yview)
        viewer.configure(yscrollcommand=scrollbar.set)
        viewer.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for row in rows:
            viewer.insert("end", f"{row.provider} | {row.kind} | {row.name} | {row.state} | {row.detail}\n")
        viewer.configure(state="disabled")
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=8)

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
        if approval == "auto_accept_trusted":
            # The auto-edit trust list is shared: Claude-only auto mode needs it too.
            codex_needs_trust = not self.codex_trust.is_trusted(self.workspace)
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
        if mode != "gpt_only" and self._claude_credit_billed(selected_claude) \
                and self._new_claude_session(selected_claude) and self.claude_consent is None:
            turns = ("\n\nIn collaboration this one session can run up to 3 Claude turns: role planning, "
                     "a draft or review, and a final review." if mode == "collaboration" else "")
            if not messagebox.askyesno("Fable credits", f"{selected_claude} may consume usage credits in a headless "
                                       "session, without Claude Code's own consent prompt." + turns +
                                       "\n\nAllow it for this one Claude session? A new or resumed session asks again."):
                return
            self.claude_consent = CreditConsent(confirmed_by_user=True, model=selected_claude)
        if self.pending_handoff is not None:
            if not self._dispatch_pending_handoff(prompt, mode):
                return
        self.prompt.delete("1.0", "end")
        self._line(f"\nUSER ({mode}): {prompt}\n")
        if self.conversation is not None:
            self.conversation.record_user(prompt, mode=mode, providers={
                "gpt_only": ["codex"], "claude_only": ["claude"]}.get(mode, ["claude", "codex"]))
        model, effort = self.codex_model.get(), self.codex_effort.get()
        self._work(lambda: self._run_turn(mode, approval, prompt, model, effort, selected_claude))

    def _run_turn(self, mode: str, approval: str, prompt: str, model: str,
                  effort: str, claude_model: str) -> None:
        try:
            self._route_turn(mode, approval, prompt, model, effort, claude_model)
        finally:
            # A yes to Fable is for the session started by THIS send; never let it carry over.
            self.claude_consent = None

    def _route_turn(self, mode: str, approval: str, prompt: str, model: str,
                    effort: str, claude_model: str) -> None:
        if mode == "gpt_only":
            turn = self._codex_turn(prompt, model, effort, approval, "answer")
            self.messages.put(("verdict", (turn, "Codex")))
        elif mode == "claude_only":
            turn = self._claude_turn(prompt, claude_model, "answer", approval)
            self.messages.put(("verdict", (turn, "Claude")))
        else:
            self._collaborate(prompt, model, effort, claude_model, approval)

    def _codex_turn(self, text: str, model: str, effort: str, approval: str, role: str) -> TurnRecord:
        assert self.codex and self.workspace
        runtime = self.codex
        if runtime.thread_id is None or runtime.workspace != self.workspace or runtime.approval_mode != approval:
            runtime.open_thread(self.workspace, approval_mode=approval, trusted_folders=self.codex_trust)
        turn = TurnRecord("codex", model)
        self.messages.put(("line", f"\nCODEX {role.upper()} / {model} / native thread {runtime.thread_id}\n"))
        if approval == "ask_every_edit" and role in {"answer", "draft, unapproved"}:
            text += ("\n\nIf this request needs file edits, do not write files. Propose each edit in one "
                     "fenced JSON block whose opening line is exactly ```codex-edits, with {\"edits\":[{\"path\":\"relative/posix/path\","
                     "\"old_text\":\"exact unique existing text, or null for a new file\","
                     "\"new_text\":\"replacement text, or null to delete an entire file\"}]}. "
                     "Use at most one edit per file. The host will show a diff and ask separately before applying "
                     "each edit. If no file edits are needed, answer normally without this block.")
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
            if role.startswith("planning") and event.kind == "tool_started":
                try:
                    runtime.interrupt()
                except Exception:
                    pass
            if event.kind in BLOCKING_KINDS:
                if event.kind == "approval_request" and event.data.get("family") in {"command", "file_change"}:
                    decision = "decline" if role.startswith("planning") else self._ask("codex_approval", (event, approval))
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
                self._remember(turn, role)
                self.history.append(turn)
                if turn.ok and approval == "ask_every_edit" and role == "answer":
                    self._review_codex_edits(turn)
                return turn

    def _review_codex_edits(self, turn: TurnRecord) -> None:
        assert self.workspace is not None
        try:
            staged = stage_proposals(self.workspace, turn.text)
        except EditProposalError as exc:
            self.messages.put(("line", f"\n[Codex edit proposal rejected: {exc}. No files applied.]\n"))
            return
        for edit in staged:
            if not self._ask("edit_review", edit):
                self.messages.put(("line", f"\n[Declined edit: {edit.relative}]\n"))
                continue
            try:
                edit.apply()
            except EditProposalError as exc:
                self.messages.put(("line", f"\n[Edit not applied: {edit.relative}: {exc}]\n"))
            else:
                self.messages.put(("line", f"\n[Applied approved edit: {edit.relative}]\n"))

    def _claude_credit_billed(self, model: str) -> bool:
        # The live menu knows when "default" currently resolves to Fable; the adapter re-checks at spawn.
        rows = self.claude_preflight.models if self.claude_preflight else []
        return any(row.get("value") == model and row.get("credit_billed") for row in rows) or is_credit_billed(model)

    def _new_claude_session(self, model: str) -> bool:
        return (self.claude_session is None or self.claude_model != model
                or self.claude_session.process.poll() is not None
                or getattr(self.claude_session, "needs_restart", False))

    def _claude_turn(self, text: str, model: str, role: str, approval: str = "ask_every_edit") -> TurnRecord:
        assert self.claude_preflight and self.workspace
        if self._new_claude_session(model):
            previous = self.claude_session
            native = previous.session_ref if previous else None
            if previous:
                previous.close()
                self.claude_session = None
            # The adapter refuses a credit-billed model without this consent, before spawning.
            consent, self.claude_consent = self.claude_consent, None
            self.claude_session = ClaudeSession(
                self.claude_preflight, self.workspace, model=model,
                resume=native, trust=self.claude_trust, credit_consent=consent,
                approval_mode=approval, auto_trust=self.codex_trust)
            self.claude_model = model
        session = self.claude_session
        if session.approval_mode != approval:
            session.set_approval_mode(approval, self.codex_trust)  # between turns only
        turn = TurnRecord("claude", model)
        self.messages.put(("line", f"\nCLAUDE {role.upper()} / {model} / {approval} / native session {session.session_ref}\n"))
        session.send(text)
        for event in session.events():
            self.messages.put(("event", event))
            if turn.finished:
                continue  # a runtime error already failed the turn; drain to the CLI's own end
            turn.append(event)
            # Auto-accepted edits are already answered by the adapter; only "ask" blocks on the user.
            if role.startswith("planning") and event.kind == "tool_started":
                session.fail_turn()
            if event.kind == "approval_request" and event.data.get("policy") == "ask":
                decision = False if role.startswith("planning") else self._ask("claude_approval", event)
                session.answer_approval(str(event.data["request_id"]), allow=decision is True)
            if turn.finished and event.kind == "runtime_error":
                session.fail_turn()  # interrupt, and decline any request still in flight
        self._remember(turn, role)
        self.history.append(turn)
        return turn

    def _remember(self, turn: TurnRecord, role: str) -> None:
        if self.conversation is not None:
            self.conversation.record_turn(turn, role=role)

    def _collaborate(self, prompt: str, model: str, effort: str,
                     claude_model: str, approval: str) -> None:
        state = Collaboration()
        try:
            def provider_turn(provider: str, text: str, role: str, mode: str = approval) -> TurnRecord:
                return (self._codex_turn(text, model, effort, mode, role) if provider == "codex"
                        else self._claude_turn(text, claude_model, role, mode))

            planning_prompt = (
                "Choose which provider should lead this task and which should give the final review, "
                "using their strengths. Codex and Claude must later approve the same answer. "
                "Do not use tools, edit files or contact connectors. Reply with ONLY JSON: "
                '{"lead":"codex|claude","final":"codex|claude","reason":"brief explanation"}. '
                f"Task: {prompt}")
            plans: dict[str, RolePlan | None] = {}
            for provider in ("codex", "claude"):
                turn = provider_turn(provider, planning_prompt, "planning, unapproved", "ask_every_edit")
                try:
                    if not turn.ok or any(event.kind in {"tool_started", "tool_finished", "approval_request"}
                                          for event in turn.events):
                        raise ValueError("Planning turn failed or used tools")
                    plans[provider] = parse_role_plan(turn.text)
                except ValueError:
                    plans[provider] = None
            codex_plan, claude_plan = plans["codex"], plans["claude"]
            if codex_plan and claude_plan and (codex_plan.lead, codex_plan.final) == (claude_plan.lead, claude_plan.final):
                plan = codex_plan
                self.messages.put(("line", f"\n[Role agreement: {plan.lead} leads; {plan.final} gives final review. "
                                            f"Codex: {codex_plan.reason} Claude: {claude_plan.reason}]\n"))
            else:
                choice = self._ask("role_decision", plans)
                plan = plans.get(choice) if choice else None
                if plan is None:
                    state.unavailable()
                    self.messages.put(("joint", state))
                    return
                self.messages.put(("line", f"\n[User chose {choice}'s role plan after disagreement: "
                                            f"{plan.lead} leads; {plan.final} gives final review.]\n"))
            lead_provider, final_provider = plan.lead, plan.final
            state.final_provider = final_provider
            partner_provider = "claude" if lead_provider == "codex" else "codex"
            lead = provider_turn(lead_provider, prompt, "draft, unapproved")
            if not lead.ok or not lead.text.strip():
                state.unavailable()
                self.messages.put(("joint", state))
                return
            if lead_provider == "codex" and approval == "ask_every_edit":
                try:
                    # Validate the complete proposal before asking the partner to approve it.
                    # Applying it still waits for both votes and each user's file decision.
                    stage_proposals(self.workspace, lead.text)
                except EditProposalError as exc:
                    self.messages.put(("line", f"\n[Codex edit proposal rejected: {exc}. No files applied.]\n"))
                    state.unavailable()
                    self.messages.put(("joint", state))
                    return
            state.propose(lead.text, lead_provider)
            packet = self._curated_handoff(lead_provider, partner_provider, prompt, candidate=state.candidate)
            if packet is None:
                state.unavailable()
                self.messages.put(("joint", state))
                return
            digest = hashlib.sha256(state.candidate.encode("utf-8")).hexdigest()
            for reviewer in ((partner_provider, lead_provider) if final_provider == lead_provider
                             else (lead_provider, partner_provider)):
                # The partner gets the curated context plus the candidate verbatim: both providers
                # approve its exact text, so it can be neither trimmed nor redacted (VLI-160).
                review_text = (f"Review this proposed answer and workspace evidence. The handoff is user-approved context, "
                               f"not native session transfer.\n\n{packet}\n\n"
                               f"## Candidate answer (exact text under review)\n{state.candidate}\n\n"
                               if reviewer == partner_provider
                               else "Review your earlier candidate against the workspace evidence again.\n\n")
                review_text += (f"Candidate SHA-256: {digest}\nReply exactly APPROVE {digest} only if you agree "
                                "this exact candidate is the best answer; otherwise reply DISAGREE and explain why. "
                                "Do not modify files in this review.")
                vote = provider_turn(reviewer, review_text, "final review, unapproved" if reviewer == final_provider
                                     else "review, unapproved")
                state.vote(reviewer, approves=vote.ok and vote.text.strip() == f"APPROVE {digest}",
                           reviewed_text=state.candidate)
                if state.state == "needs_user_decision":
                    break
            if state.joint_answer is not None and lead_provider == "codex" and approval == "ask_every_edit":
                self._review_codex_edits(lead)
            self.messages.put(("joint", state))
        except Exception:
            state.unavailable()
            self.messages.put(("joint", state))
            raise

    def _curated_handoff(self, source: str, target: str, summary: str, *, candidate: str | None = None,
                         reason: str | None = None, record: bool = True) -> str | None:
        """Build the draft from the shared conversation and let the user curate it.

        Collaboration sends the packet immediately, so it is recorded here. A manual handoff
        (record=False) is recorded by _send when, and exactly as, it is sent.
        """
        assert self.conversation is not None
        draft = draft_handoff(self.conversation, source=source, target=target, user_summary=summary,
                              reason=reason)
        if candidate is not None:
            draft.items = [item for item in draft.items if item.id != "reply"]  # sent verbatim instead
        # The Hand off button runs on the Tk thread, where waiting on the UI queue would deadlock.
        on_ui_thread = getattr(self, "_ui_thread", None) is threading.current_thread()
        result = (self._dialog("handoff", (draft, candidate)) if on_ui_thread
                  else self._ask("handoff", (draft, candidate)))
        if result is None:
            return None
        packet, counts = result
        if record:
            draft.record(self.conversation, packet, counts)
        else:
            self.pending_handoff = draft
        return packet

    def _dispatch_pending_handoff(self, prompt: str, mode: str) -> bool:
        """Scan the text the user is actually sending and record it. False keeps it unsent."""
        draft, self.pending_handoff = self.pending_handoff, None
        assert draft is not None and self.conversation is not None
        target_modes = {"claude": ("claude_only", "collaboration"), "codex": ("gpt_only", "collaboration")}
        if mode not in target_modes[draft.target]:
            return True  # the user changed plan; this is an ordinary turn, not the handoff
        try:
            final, counts = draft.finalize(prompt)
        except HandoffNeedsReview as needs:
            if not messagebox.askyesno("Still looks secret", "The text you are sending still contains: "
                                       f"{describe_redactions(needs.counts)}\n\nSend it anyway, unredacted?"):
                self.pending_handoff = draft  # keep it pending so the next Send checks again
                return False
            final, counts = draft.finalize(prompt, send_despite_findings=True)
        draft.record(self.conversation, final, counts)
        return True

    def _handoff_manual(self) -> None:
        """Switch provider with a reviewed packet. Nothing is sent until the user presses Send."""
        if self.busy or self.conversation is None:
            return
        last = self.conversation.last_turn()
        if last is None:
            messagebox.showinfo("Nothing to hand off", "Run a turn first; the handoff is built from it.")
            return
        source = last.provider or "codex"
        target = "claude" if source == "codex" else "codex"
        summary = self.prompt.get("1.0", "end").strip() or "Continue this work from the handoff below."
        packet = self._curated_handoff(source, target, summary, record=False)
        if packet is None:
            return
        self.mode.set("claude_only" if target == "claude" else "gpt_only")
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", packet)
        self.status.set(f"Handoff ready for a new {target.title()} conversation. Review it and press Send turn.")

    def _show_conversation(self) -> None:
        if self.conversation is None:
            messagebox.showinfo("No project", "Choose a project first.")
            return
        log = self.conversation
        window = tk.Toplevel(self.root)
        window.title("Shared conversation (local, private)")
        window.geometry("820x600")
        ttk.Label(window, text="Stored only on this PC. Claude and Codex keep their own native sessions; "
                               "this record is the desktop's own.", wraplength=780).pack(anchor="w", padx=8, pady=4)
        view = tk.Text(window, wrap="word")
        view.insert("1.0", log.render())
        view.configure(state="disabled")
        view.pack(fill="both", expand=True, padx=8)
        row = ttk.Frame(window)
        row.pack(fill="x", padx=8, pady=6)

        def export() -> None:
            target = filedialog.asksaveasfilename(parent=window, title="Export redacted conversation",
                                                  defaultextension=".txt", filetypes=[("Text", "*.txt")])
            if target:
                Path(target).write_text(log.render(redacted=True), encoding="utf-8")

        def start_new() -> None:
            if self.workspace is not None:
                self.conversation = ConversationLog.start_new(private_state_dir(), self.workspace)
                window.destroy()
                self._line("\n[Started a new shared conversation for this project.]\n")

        def forget() -> None:
            if messagebox.askyesno("Forget conversation", "Delete this conversation's local record? "
                                   "Provider-native sessions are not affected.", parent=window):
                log.forget()
                window.destroy()

        ttk.Button(row, text="Export redacted copy...", command=export).pack(side="left")
        ttk.Button(row, text="Start new conversation", command=start_new).pack(side="left", padx=6)
        ttk.Button(row, text="Forget this conversation", command=forget).pack(side="right")

    def _handoff_dialog(self, draft: HandoffDraft, candidate: str | None) -> tuple[str, dict[str, int]] | None:
        window = tk.Toplevel(self.root)
        window.title(f"Review handoff: {draft.source.title()} -> {draft.target.title()}")
        window.geometry("900x620")
        ttk.Label(window, text="Choose what crosses to the other provider, edit it, then send. Secrets, e-mail "
                               "addresses and home-folder names are redacted. Native sessions stay separate.",
                  wraplength=860).pack(anchor="w", padx=8, pady=4)
        body = ttk.Frame(window)
        body.pack(fill="both", expand=True, padx=8)
        picks = ttk.Frame(body)
        picks.pack(side="left", fill="y")
        editor = tk.Text(body, wrap="word")
        editor.pack(side="right", fill="both", expand=True)
        report = tk.StringVar()
        flags: dict[str, tk.BooleanVar] = {}

        def refresh() -> None:
            for item_id, flag in flags.items():
                if not draft.item(item_id).required:
                    draft.set_included(item_id, flag.get())
            packet, counts = draft.render()
            editor.delete("1.0", "end")
            editor.insert("1.0", packet)
            note = describe_redactions(counts)
            if candidate is not None:
                findings = redact(candidate)[1]
                note += (" The candidate answer is sent in full and unmodified for exact approval"
                         + (f"; it contains: {describe_redactions(findings)}" if findings else "."))
            report.set(note)

        for item in draft.items:
            flags[item.id] = tk.BooleanVar(value=item.included)
            ttk.Checkbutton(picks, text=item.label, variable=flags[item.id], command=refresh,
                            state="disabled" if item.required else "normal").pack(anchor="w")
        ttk.Label(window, textvariable=report, wraplength=860).pack(anchor="w", padx=8)
        result: dict[str, Any] = {"value": None}

        def send() -> None:
            text = editor.get("1.0", "end").strip()
            findings = redact(candidate)[1] if candidate is not None else {}
            if findings and not messagebox.askyesno(
                    "Candidate contains secrets",
                    f"The candidate answer contains: {describe_redactions(findings)}\n\nIt is sent verbatim "
                    f"to {draft.target.title()}, because both providers must approve its exact text. Send it "
                    "anyway? No goes back to the review; Cancel leaves the draft unapproved.", parent=window):
                return
            try:
                result["value"] = draft.finalize(text)
            except HandoffNeedsReview as needs:
                if not messagebox.askyesno("Still looks secret", f"Your edited text still contains: "
                                           f"{describe_redactions(needs.counts)}\n\nSend it anyway, unredacted?",
                                           parent=window):
                    return
                result["value"] = draft.finalize(text, send_despite_findings=True)
            window.destroy()

        buttons = ttk.Frame(window)
        buttons.pack(fill="x", padx=8, pady=6)
        ttk.Button(buttons, text="Rebuild from selection (discards text edits)", command=refresh).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="Send reviewed handoff", command=send).pack(side="right", padx=6)
        refresh()
        window.transient(self.root)
        window.grab_set()
        self.root.wait_window(window)
        return result["value"]

    def _interrupt(self) -> None:
        if self.codex and self.codex.active_turn_id:
            threading.Thread(target=self.codex.interrupt, daemon=True).start()
        if self.claude_session:
            session = self.claude_session

            def stop() -> None:
                # Stop fails the turn: an auto-accept edit arriving after Stop is declined too.
                session.fail_turn()
            threading.Thread(target=stop, daemon=True).start()

    def _dialog(self, kind: str, data: Any) -> Any:
        if kind == "edit_review":
            edit: StagedEdit = data
            window = tk.Toplevel(self.root)
            window.title(f"Review one Codex edit: {edit.relative}")
            window.geometry("900x650")
            ttk.Label(window, text=f"Apply this one change to {edit.relative}? Review the complete diff.",
                      wraplength=850).pack(anchor="w", padx=8, pady=8)
            if is_agent_config_path(edit.relative):
                ttk.Label(window, text="AGENT CONFIGURATION WARNING: This file can change future hooks, "
                          "MCP tools, permissions or CI actions. Apply only if you intend that change.",
                          wraplength=850, foreground="#9a3412",
                          font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=8, pady=4)
            diff_frame = ttk.Frame(window)
            diff_frame.pack(fill="both", expand=True, padx=8)
            diff_view = tk.Text(diff_frame, wrap="none")
            vertical = ttk.Scrollbar(diff_frame, orient="vertical", command=diff_view.yview)
            horizontal = ttk.Scrollbar(diff_frame, orient="horizontal", command=diff_view.xview)
            diff_view.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            diff_view.insert("1.0", edit.diff)
            diff_view.configure(state="disabled")
            diff_view.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            diff_frame.rowconfigure(0, weight=1)
            diff_frame.columnconfigure(0, weight=1)
            result: dict[str, bool] = {"value": False}
            buttons = ttk.Frame(window, padding=8)
            buttons.pack(fill="x")
            def choose(allow: bool) -> None:
                result["value"] = allow
                window.destroy()
            ttk.Button(buttons, text="Apply this edit", command=lambda: choose(True)).pack(side="right")
            ttk.Button(buttons, text="Decline", command=lambda: choose(False)).pack(side="right", padx=8)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["value"]
        if kind == "role_decision":
            window = tk.Toplevel(self.root)
            window.title("Choose collaboration roles")
            window.geometry("650x330")
            ttk.Label(window, text="The providers did not agree on who leads and gives the final review. "
                      "Choose a valid proposal or leave the task pending.", wraplength=620).pack(anchor="w", padx=12, pady=12)
            result: dict[str, str | None] = {"value": None}
            def choose(provider: str | None) -> None:
                result["value"] = provider
                window.destroy()
            for provider in ("codex", "claude"):
                plan = data.get(provider)
                if plan is not None:
                    ttk.Button(window, text=f"Use {provider.title()} plan: {plan.lead} leads, {plan.final} final",
                               command=lambda selected=provider: choose(selected)).pack(fill="x", padx=12, pady=4)
                    ttk.Label(window, text=plan.reason, wraplength=620).pack(anchor="w", padx=20)
                else:
                    ttk.Label(window, text=f"{provider.title()} did not provide a valid role plan.").pack(anchor="w", padx=12)
            ttk.Button(window, text="Leave pending", command=lambda: choose(None)).pack(fill="x", padx=12, pady=8)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["value"]
        if kind == "handoff":
            draft, candidate = data
            return self._handoff_dialog(draft, candidate)
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
                messagebox.showwarning("Read-only Codex execution",
                                       f"Codex requested permission beyond read-only execution:\n\n{detail}\n\nUse a codex-edits proposal for the host-owned review path. This request will be declined.")
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
                    elif event.kind in {"rate_limit", "runtime_error", "process_exited", "notice", "tool_started",
                                        "tool_finished", "approval_decision", "session_started"}:
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
                    pre = self.claude_preflight
                    version = ".".join(map(str, pre.version)) if pre and pre.version else "unknown"
                    self._line(f"\nClaude connected. CLI {version} ({pre.version_status if pre else 'unknown'}), "
                               f"plan {pre.account.get('plan') if pre else 'unknown'}. {len(models)} menu choices:\n")
                    for row in data:
                        credit = "  [asks before use: may bill usage credits]" if row.get("credit_billed") else ""
                        target = row.get("resolved_model")
                        target = f" -> {target}" if target not in (None, row["value"]) else ""
                        self._line(f"  {row['value']}{target} ({row.get('display_name')}){credit}\n")
                elif kind == "capabilities":
                    self._show_capabilities(data)
                    self.status.set(f"Inspected {len(data)} local capability rows. Tool calls remain unverified.")
                elif kind == "verdict":
                    turn, owner = data
                    self._line(f"\n[{owner} {'answer complete' if turn.ok else 'failed or incomplete: ' + str(turn.failure)}]\n")
                    if not turn.ok:
                        other = "Claude" if owner == "Codex" else "Codex"
                        self._line(f"[Hand off... can move this work to {other} with a packet you review first.]\n")
                elif kind == "joint":
                    state: Collaboration = data
                    if self.conversation is not None:
                        self.conversation.append("collaboration", {
                            "state": state.state, "lead": state.source_provider,
                            "votes": {name: vote[0] for name, vote in state.votes.items()}})
                    if state.joint_answer is not None:
                        self._line(f"\n[Jointly approved by Claude and Codex; final review by "
                                   f"{state.final_provider or 'unspecified provider'}]\n{state.joint_answer}\n")
                    else:
                        self._line("\n[Collaboration unresolved. Drafts and reviews above are unapproved. Choose the next action.]\n")
                        choice = self._dialog("resolution", state)
                        if self.conversation is not None:
                            self.conversation.append("user_decision", {"choice": choice, "draft_of": state.source_provider})
                        if choice == "use_draft":
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
        self._drain_job = self.root.after(50, self._drain)

    def _disconnect(self) -> None:
        if self.codex_transport:
            self.codex_transport.close()
        if self.claude_session:
            self.claude_session.close()
        self.codex = self.codex_transport = self.claude_session = None
        self.claude_consent = None
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
