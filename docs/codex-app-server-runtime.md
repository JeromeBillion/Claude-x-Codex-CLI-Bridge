# Codex App Server runtime slice (VLI-158/159)

Status: implemented adapter foundation with fake-server tests; **not validated against Jerome's signed-in Windows CLI**. The existing `bridge.ps1` / `tools/agent_bridge.py` remains the standalone CLI mode.

## Boundary

The personal Windows desktop host spawns the installed `codex app-server` as a child and exchanges newline-delimited JSON-RPC over stdio. The host never reads Codex's credential files, receives OAuth tokens, opens an API billing fallback, or exposes the experimental WebSocket listener. Codex owns ChatGPT sign-in and token refresh. `account/read` must return a ChatGPT account before this adapter starts subscription turns. The plan category is displayed; email and account IDs are not persisted by this adapter.

The adapter has three interfaces:

| Interface | Responsibility | Current code |
| --- | --- | --- |
| `AppServerTransport` | Spawn process, handshake messages, correlate responses, queue notifications and server requests | `tools/codex_app_server.py` |
| `CodexRuntime` | Discover account/catalog/limits; start/resume a thread; start/steer/interrupt turns; normalize UI events; require explicit command/file approval decisions | Same module |
| `ThreadStore` | Persist workspace → native Codex thread ID in a caller-chosen private local JSON file, atomically | Same module |

The UI owns rendering, user decisions, drafts, workspace choice and application lifecycle. A future Claude adapter should emit the same basic envelope (`provider`, native thread ID, `kind`, `turnId`, payload) without pretending that provider histories are interchangeable. Handoff is a **user-visible summary plus selected file paths/diffs**, reviewed before sending as a new input to the other provider. Never send the other provider's hidden transcript or automatically mirror tool outputs. Native Codex threads stay in Codex storage; the map is only a pointer. Consumer ChatGPT chats are outside this interface.

## Lifecycle and safety

1. Launch installed executable with `shell=False`; `initialize` and `initialized`; call `account/read` and reject non-ChatGPT auth. Then paginate `model/list` and fetch `account/rateLimits/read`. Render only visible returned models and their returned efforts. A listed model is a candidate, not a proven entitlement: a small turn must succeed on Jerome's account.
2. Open a local workspace thread in `readOnly` by default or `workspaceWrite` only after a clear UI choice. Preserve `approvalPolicy: onRequest`. Save the returned native ID, and call `thread/resume` after process restart. A missing/invalid stored thread is shown as a recoverable error; no silent recreation of its history.
3. Select model and effort per `turn/start`; stream delta, item and completion events. `turn/steer` targets the active turn ID; `turn/interrupt` includes that ID. Completion is authoritative. Rate-limit updates and `error` / failed turn details are surfaced; known usage/auth/connection error kinds are mapped without switching billing paths.
4. Server-initiated requests block progress. The first slice renders all requests as `approval_required`, but permits a decision only for command/file requests after a user action. Other request families (permission subsets, MCP elicitation, app approval) need dedicated forms and response validation; until then the UI must interrupt or keep the turn blocked. `thread/shellCommand` and direct process APIs are not in the transport allowlist. The probe declines command/file requests and interrupts on others.
5. Keep Windows sandbox setup visible. Windows 11 native PowerShell/elevated sandbox is the preferred target; use WSL2 only as an intentional environment switch. Do not turn a failed sandbox setup into full-access execution. Display tool output/diffs for review, then request user approval according to Codex's policy.

## First vertical slice

Wire the adapter into a small desktop view with a local workspace picker, account plan status, dynamic model/effort picker, one read-only thread, streaming text and item timeline, command/file approval modal, cancel/steer controls, and resume after app restart. Keep a button to launch the existing `bridge.ps1` flow. No shared login screen, plugin installer, connector parity claim, or API-key billing in this slice.

## Acceptance gates

- Fake-server tests: handshake and response correlation; blocked `thread/shellCommand`; catalog pagination and model/effort validation; read-only thread creation and native-ID resume; stream mapping; explicit decline and unsupported approval blocking; API-key account rejection; usage-limit error mapping. Run `python -m unittest discover -s tools/tests -p "test_*.py" -v`.
- Windows probe on Jerome's own signed-in machine: from PowerShell, run `python -m tools.codex_probe` for CLI version, version-matched schema generation, ChatGPT plan category, visible model IDs/efforts and rate-limit presence. No account email/ID, credential, thread ID, raw message or model output is printed. Run `python -m tools.codex_probe --turn` only when ready for two tiny subscription-backed, read-only calls in a disposable temporary folder; it verifies the selected model, streamed completion and native thread resume across process restart. Keep output local or share only its sanitized stdout. The probe does not test Windows sandbox elevation or every model.
- On Windows, test a controlled command approval and a proposed file change in a disposable Git repo: display request, decline, then accept on a separate turn; ensure no write after decline. Test interruption and offline/expired-login/usage-limit behavior without recording account identifiers. Run the full suite and a manual UI smoke test before claiming the desktop product works end to end.

## Blocking questions for VLI-158/159

1. Which model IDs and effort levels does Jerome's `model/list` return under his ChatGPT account, and which actually complete a turn (especially Astra and Sol)? What does `account/rateLimits/read` report?
2. Does the installed Windows CLI support the current stable App Server schema, and does native Windows sandbox setup work in his environment? Generate schemas from **that installed version**, then compare the request/approval shapes against this adapter before enabling writes.
3. How will the UI handle the remaining blocking requests (permission subset, MCP forms, connector approval), and which configured skills/MCP/connectors are callable in this client? Do not use App Server `plugin/list/read/install`, which the documentation marks under development for production clients.
4. Which files/diffs and summary may be handed to Claude, and what should the user review at provider switch? The explicit handoff format is still a product decision.

Official references: [App Server protocol](https://learn.chatgpt.com/docs/app-server), [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox), [pricing and limits](https://learn.chatgpt.com/docs/pricing), [Projects and chats](https://learn.chatgpt.com/docs/projects).
