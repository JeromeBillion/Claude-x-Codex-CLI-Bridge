# Desktop host contract for Codex and Claude lanes

`tools/desktop.py` is the shared local Tk host. It imports both runtime adapters
and their common `Envelope` from `tools/runtime_events.py`. The host has one
active turn at a time per selected project. A provider's `session_ref` is always
its native ID; the host never sends that ID to the other provider as a resume
token. `tools/desktop_state.py` owns turn and collaboration verdict state.

The host calls `CodexRuntime.initialize()`, `discover()`, `open_thread()`,
`start_turn()`, `next_event()` and `decide_approval()`. It passes the filtered
`safe_child_env()` to App Server. `discover()` pages the full `model/list`
catalog, including hidden entries, and the selected entry's callable `model`
field is passed per turn. `ThreadStore` keeps the native thread ID in the user's
local app state. The UI never substitutes a fixed model list.

For Claude, the host calls `preflight()`. Its `models` field is the live,
callable menu, with `value`, `resolved_model`, `display_name`, `effort_levels`
and `credit_billed`; `models_report` is the redacted form. The host then calls
`ClaudeSession(...).send()`, `events()` and `answer_approval()`.
- **Fable.** When the user confirms a credit-billed model (Fable, or a
  `default` that currently resolves to Fable), the host creates a
  `CreditConsent`. `ClaudeSession(credit_consent=...)` binds it to exactly one
  session. Without it, the adapter refuses before spawning. It re-checks
  against the chat process's own live menu, and `set_model` is checked too.
- **Run trust.** The existing `TrustStore` must hold the workspace before any
  headless Claude execution.
- **Approval toggle.** `ClaudeSession(approval_mode=, auto_trust=)` applies
  the toggle, and `set_approval_mode()` can change it between turns only.
  `auto_trust` is the same `TrustedFolderStore` Codex uses, so one explicit
  list of folders is trusted for automatic edits.
- **Approval events.** Every Claude `approval_request` carries
  `policy: "ask" | "auto_accept"`. The host answers only `ask` requests. An
  auto-accepted one is followed by `approval_decision` with
  `by: "auto_trusted"`, and a user answer produces `by: "user"`.

`ask_every_edit` uses Codex `readOnly` with `onRequest`. For a GPT-only answer
or a Codex-led collaboration draft,
the host asks Codex to return any desired file changes in a single
`codex-edits` JSON block. `tools/staged_edits.py` validates exact relative
paths and current text, shows a full diff for each file, and applies only that
file after a separate user approval. It rejects links, missing/ambiguous old
text, changed files, duplicate targets and oversized proposals. Unrecognized
or malformed proposals are never applied. This currently supports UTF-8 text
replacements, new files in existing directories and whole-file deletion; it
accepts LF-only proposals against uniformly CRLF files while retaining CRLF in
the result. Mixed-ending files require the proposal's exact line endings. The
diff always shows the real before and after; edits to agent configuration paths
show an extra warning before the user's per-file decision. In collaboration,
the host stages the proposal again against the live file after both votes, so
the diff presented for approval reflects any intervening workspace changes. It
does not apply binary edits, directory creation or Codex edits from a
collaboration review turn. In collaboration, proposals are validated before
partner review and applied only after both providers approve the exact draft
and the user approves each file. Unsupported cases require trusted auto
mode or handle them outside the host. Codex command/file escalation in default
mode remains displayed and declined. In trusted auto mode,
a file or command approval issued by App Server blocks the turn and requires
an explicit user answer. MCP elicitation, connector input and unknown approval families are
shown as unsupported and interrupt the turn. No silent approval is sent.

Collaboration is deliberately sequential: the user selects a lead based on the
task, that provider drafts, the user can edit or omit the handoff packet, the
partner reviews, and the lead confirms. The candidate is
jointly presented only if Claude and Codex each return an exact approval of
the same SHA-256 candidate. Any disagreement, malformed vote, failure or
unavailable provider leaves the candidate unapproved and opens a user decision.
Single-provider modes show only that provider's answer as its own.

This is local shell/UI evidence only. No live signed-in Windows model turn,
native sandbox exercise, approval accept/decline or packaged alpha has passed.
The existing PowerShell/Python CLI entrypoint remains usable.
