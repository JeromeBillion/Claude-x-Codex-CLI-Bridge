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

For Claude, the host calls `preflight()` for its dynamic model menu, then
`ClaudeSession(...).send()`, `events()` and `answer_approval()`. The host blocks
Fable or `best` until explicit per-app-session credit confirmation. It requires
the existing `TrustStore` before headless Claude execution. Claude's adapter
currently does not expose a user-selected approval policy, so the host refuses
the auto-accept choice when Claude or collaboration is selected. The Claude
lane can add a `permission_mode` argument to `ClaudeSession` with a safe default
and then connect that argument in the host. Auto-accept must remain scoped to
the exact trusted folder.

`ask_every_edit` currently uses Codex `readOnly` with `onRequest`; the timeline
states that this is proposal-only. A per-edit staged diff/accept/apply path is
still needed before calling the default mode fully functional. In this mode,
Codex command/file escalation is displayed and declined. In trusted auto mode,
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
