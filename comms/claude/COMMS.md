# Claude lane - Claude x Codex Desktop

This file is the Claude Code lane for this repo only. Codex uses `comms/codex/COMMS.md`. Project: https://linear.app/vlive-projects/project/claude-x-codex-desktop-10801153fadb

## Protocol
- Read current main; one live numbered prompt per lane. Reply below the matching prompt, with PR URL and head SHA if code was changed, tested evidence, unresolved questions and blockers. Do not rely on a stale copy, overwrite a concurrent prompt or mix in other products' work.
- Work on a branch and open a focused PR for code. On this Claude-x-Codex-CLI-Bridge repo only, Jerome authorizes autonomous testing and merging to main after your tests pass; do not merge red work or deploy. A research-only answer needs no PR. A COMMS-only reply may be committed to main with `comms: claude reply N [skip ci]` without awaiting a merge gate. Never store secrets, tokens or private transcripts in this public repo.
- Keep reviewing claims independently; a failed safety or billing test still blocks its code, even without a separate human merge gate. Claude and Codex are collaborators, not rubber stamps. Challenge a DONE claim against the repo before confirming it. Suggest useful features where tickets leave the details open, but do not silently change the locked scope: Windows desktop coding workspace, Claude Code and Codex runtime, first-class provider/model switching, local context handoff, CLI mode retained.
- Subscription access, supported model names, native chat history and connector parity are questions to prove, not facts to claim. Do not evade provider restrictions or collect user credentials.

## Current prompt - 2 (VLI-158/159, Claude runtime)
Seeded 2026-09-27 SAST. Read current main and PR #1 before starting. This is the Claude lane only.

Jerome's scope decision at 14:55 SAST, verbatim: "thats the whole product today. - build it as your personal tool first, so thats correct". Build for Jerome's own Windows desktop use with his own installed, signed-in Claude Code and Codex subscriptions. Retain CLI mode. Do not design subscription-login distribution to other users, API-key fallback, or credential interception. The Windows probe has not been run on Jerome's PC; its results will be supplied later. Do not mark his account's entitlement or model catalog proven.

First, fix your PR #1 probe before asking Jerome to run any part of it. Independent review of head a7d0ad3 found three defects:
1. `run_turn` accepts the model's `can_use_tool` Write input unchanged on its allow path. Confine any Write to the disposable probe directory, validate the actual proposed target and reject escapes, absolute outside paths, traversal and links that escape; test the guard. A claim that only Write is enabled does not confine the path.
2. Child processes inherit `ANTHROPIC_API_KEY` from the environment. Explicitly strip API-key variables from every spawned CLI environment, including version/auth probes where relevant, and fail safely if subscription auth cannot be verified; test a planted fake key never reaches the child. No API-billed turns.
3. `summarize_result` prints the first 160 characters of raw model output as `result_text`, despite the no-account-identifiers claim. Never emit untrusted free text or account identifiers in shareable probe JSON; compute bounded booleans/enum statuses internally for checks like resume and include only allowlisted structured fields. Test sensitive text does not appear in stdout/report.
Also review the rest of its output paths for the same privacy and billing issues. Update the SAME PR #1 branch, add adversarial tests, run the full test suite, and report the new head SHA, diffs and evidence. Merge PR #1 yourself only after all three defects and the broader billing/output review are fixed, adversarial tests and full suite pass; do not deploy. Until the fixed PR is verified green, Jerome should run neither `python tools\claude_runtime_probe.py` nor `--turns`.

In parallel, design Claude's desktop runtime adapter for VLI-158/159 from your prompt-1 findings. Define the process lifecycle and stream-json event mapping for text deltas, tool activity, approval requests, per-turn terminal state, `is_error` versus exit code, rate-limit events and `model_refusal_fallback` visibility. Specify `initialize.models` discovery, `set_model`, interrupt, session resume, upgrade/version guard and explicit context handoff to Codex without claiming portable native sessions or consumer-chat history. Account for Windows executable discovery, local workspace trust, installed CLI authentication, no API-key billing and no hidden subscription-credit costs. Identify a small first vertical slice, interface and acceptance tests, open questions and any provider terms that constrain this personal-use design. Feed the design into VLI-158/159, without silently changing their scope. If implementation is warranted, keep it in a focused separate PR and test and merge safe code autonomously in this repo only; do not deploy. Reply directly under prompt 2 in this file with evidence and links; do not overwrite the prompt or Codex's lane.

## Previous prompt - 1 (VLI-157)
Seeded 2026-09-27 SAST.

FOR CLAUDE CODE:
Investigate the Claude Code side of a Windows desktop workspace driving an installed, unmodified, user-authenticated CLI. Jerome wants coding and model switching with his subscription, but we must establish the exact allowed integration path rather than assume a workaround. Use current official terms and a safe reproducible probe if available. Inventory actual models (including whether his examples Fable and Opus are exposed), streaming turns, local session resume, tool approvals, plugins/MCP/connectors, usage limits and failure behavior. Distinguish Claude Code sessions from Claude.ai consumer chats. Return a permitted architecture or a clear no-go, citations and tests; propose missing product features. Do not use API keys as an unapproved product fallback, intercept tokens, merge or deploy. Ticket: https://linear.app/vlive-projects/issue/VLI-157/prove-allowed-claude-code-codex-cli-desktop-runtime-and-available

## Claude reply - 1
Replied 2026-09-27 by Claude Code (cloud session). PR: https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/1. The probe code is at `c47338e`; the PR head is the commit that adds this reply.

### Verdict
- **Conditional GO for Jerome's own use.** A local Windows workspace can drive his own installed, unmodified `claude.exe` in headless stream-json mode, with him signed in through Anthropic's own `/login`. Every capability in the prompt was reproduced over that path. Policy basis:
  - The legal page carves out "an end user signing in to the unmodified Claude Code binary with their own Claude subscription".
  - Plan limits "assume ordinary, individual usage of Claude Code and the Agent SDK".
- **NO-GO for shipping to other users on subscription login without written approval from Anthropic.** Direct quotes:
  - "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users."
  - "developers may not collect, store, or intermediate Claude.ai credentials or session tokens".
  - Agent SDK docs: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK."
  - "preinstalling or running Claude Code in your products or services ... requires agreeing to our Commercial Terms".
- A product for other users needs one of two things: each user brings their own API key or cloud-provider credential (Anthropic's documented path), or Anthropic signs off in writing. The legal page points to sales for this. That call is Jerome's to make; this lane does not assume it.

### Permitted architecture (Claude side)
1. **Binary.** Spawn the user's own CLI, found on PATH or at `%USERPROFILE%\.local\bin\claude.exe`. With the Agent SDK, set `pathToClaudeCodeExecutable` / `cli_path`, because by default the SDK runs its own bundled binary. Never modify or patch the CLI.
2. **Auth.** Never pass `--bare`. In bare mode the CLI never reads OAuth or the keychain, and the docs say bare "will become the default for `-p` in a future release". Guard against that in CI and on every CLI upgrade.
   - Never read `.credentials.json`, never call `setup-token` for the user, never store tokens.
   - Readiness check: the exit code of `claude auth status` (0 = logged in, 1 = not).
   - Not logged in: send the user to `claude auth login` in their own terminal.
   - If `ANTHROPIC_API_KEY` is present in the environment the CLI silently bills the API instead. Warn the user and never set it ourselves.
3. **Transport.** One long-lived process per chat:
   `claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages --permission-prompt-tool stdio --permission-mode default --session-id <uuid>`
   - Send `control_request` `initialize` first. Its response carries `account.subscriptionType` and a `models[]` menu.
   - `set_model` switches the model mid-session.
   - `interrupt` implements Stop.
   - Answer each `can_use_tool` request with `control_response` allow or deny.
   - Reopen a chat with `--resume <uuid>`; branch one with `--fork-session`.
4. **Model switcher.** Build it from `initialize.models` or the SDK's `supportedModels()`. Never hard-code names. Switching between Claude and Codex is a context handoff: the app writes a summary into the other runtime's new session. Session IDs are not portable between them.
5. **History.** The CLI owns its transcripts at `~/.claude/projects/<cwd-slug>/<id>.jsonl`. The format is internal and changes between versions, and files are pruned after 30 days. The app should keep its own rendered log of stream events and treat CLI session IDs as resume handles only.

### Evidence (CLI 2.1.283, `python tools/claude_runtime_probe.py --turns`, plus manual probes)
Caveat first: these ran in a Linux cloud container. It signs in with a host-provided OAuth token whose plan type is `Claude API`, not a Pro/Max plan. The plan-specific rows are **not proven for Jerome's account** until he runs the probe on Windows.

| Area | Observed |
|---|---|
| Models | Menu offered: `default` (Sonnet 5), `sonnet` (`claude-sonnet-5`), `claude-fable-5-1` ("Fable"), `opus` (`claude-opus-5-5`), `haiku` (`claude-haiku-4-5-20251001`). All served with `--model`: aliases `fable`→5.1, `opus`→Opus 5.5, `opusplan`, plus full IDs including `claude-fable-5`. **Fable and Opus are exposed. "Astra" is not a Claude model.** `set_model` in the middle of a session switched the serving model from Haiku to Sonnet. |
| Streaming | Deltas stream as `stream_event` `text_delta` / `thinking_delta`, and each turn ends with a `result` line. |
| Resume | `--session-id X`, then `--resume X` in a new process, correctly recalled the codeword. |
| Approvals | With stdio approvals, allow wrote the file and deny left no file (`permission_denials: 1`). With `--permission-prompts none`, any tool that needs approval is denied automatically. |
| Interrupt | `interrupt` gives `result` `error_during_execution`, `terminal_reason: aborted_streaming`, exit code 1. |
| MCP | A throwaway stdio MCP server loaded with `--mcp-config` and `--strict-mcp-config` connected, and its tool was called in `-p`. |
| Limits | Each turn emits a `rate_limit_event` with `status`, `rateLimitType` (`five_hour`), utilization for the `five_hour` and `seven_day` windows, and overage state. |
| Failures | An unknown model gives exit 1, `is_error: true`, `api_error_status: 404`, **but `subtype: "success"`**, so hosts must check `is_error`. `claude-fable-5` on a trivial prompt triggered a safety-classifier stop, then `system/model_refusal_fallback` to `claude-opus-4-8`, still with exit 0. This happened once; it shows the fallback event exists, not how often it fires. |

Doc facts not probed here:
- Assistant message `error` codes include `authentication_failed`, `rate_limit`, `billing_error` and `oauth_org_not_allowed`.
- Result subtypes include `error_max_turns` and `error_max_budget_usd`.
- Limit messages look like "You've hit your session limit · resets …".

### Claude Code vs Claude.ai consumer chats
- Claude Code sessions live in local JSONL on the machine. The docs say the desktop app, claude.ai/code and the VS Code extension each keep their own session history.
- No documented CLI or API reads consumer claude.ai chat history, so importing native chats is **not available**.
- It is also not proven that CLI sessions never appear in claude.ai. No doc says so either way.
- What is shared:
  - Usage limits are shared across Claude and Claude Code.
  - claude.ai connectors auto-load into Claude Code, but only with a subscription `/login`. They do not load with an API key or `CLAUDE_CODE_OAUTH_TOKEN`, and they can be disabled.
- Cloud sessions (`--cloud`, `--teleport`) are a separate, one-way handoff and out of scope for v1.

### Challenges to the current bridge (on main)
- `DEFAULT_CLAUDE_MODEL = "claude-fable-5"` pins an older model; the `fable` alias now resolves to 5.1. That pin was also the model behind the refusal fallback seen above.
- `--output-format text` hides `is_error`, `terminal_reason`, rate-limit state and fallback notices from the bridge.
- `--permission-mode auto` with no way to show approvals does not work for a desktop app that needs approvals.
- `--no-session-persistence` rules out native resume.
- All four are acceptable for a CLI MVP, but they must change for the desktop runtime.

### Proposed features
- A trust prompt the app shows itself before the first run in each folder. `-p` skips the CLI's trust dialog and runs that repo's hooks and `.mcp.json` without asking.
- A confirmation before Fable turns that may bill usage credits. `-p` never shows Fable's consent prompt and "bills it without asking".
- A usage meter driven by `rate_limit_event`, with a warning at `allowed_warning` and a clear banner at `rejected`.
- Model-fallback and refusal notices shown in the chat.
- An upgrade guard that re-runs the probe after each CLI update, catching the `--bare` default change and changes to the model menu.

### Open questions / blockers
1. **Jerome:** run `python tools\claude_runtime_probe.py` on the Windows machine, then add `--turns`. That proves his plan type, his model menu (Fable is plan-dependent) and Windows `.exe` spawning. Paste only the redacted JSON it prints.
2. **Product:** personal tool only, or distribution? Distribution needs Anthropic's written approval, or API keys supplied by each user.
3. **Codex lane:** please confirm whether the Codex app-server offers equivalents of `initialize.models`, `set_model` and interrupt. That decides whether one model-switcher abstraction can cover both runtimes.

Citations:
- https://code.claude.com/docs/en/legal-and-compliance
- https://code.claude.com/docs/en/agent-sdk/overview
- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/model-config
- https://code.claude.com/docs/en/sessions
- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/errors
- https://code.claude.com/docs/en/authentication
- https://code.claude.com/docs/en/setup
- https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan
- https://www.anthropic.com/legal/consumer-terms
