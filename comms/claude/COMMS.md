# Claude lane - Claude x Codex Desktop

This file is the Claude Code lane for this repo only. Codex uses `comms/codex/COMMS.md`. Project: https://linear.app/vlive-projects/project/claude-x-codex-desktop-10801153fadb

## Protocol
- Read current main; one live numbered prompt per lane. Reply below the matching prompt, with PR URL and head SHA if code was changed, tested evidence, unresolved questions and blockers. Do not rely on a stale copy, overwrite a concurrent prompt or mix in other products' work.
- Work on a branch and open a focused PR for code. Do not merge or deploy. A research-only answer needs no PR. A COMMS-only reply may be committed to main with `comms: claude reply N [skip ci]` if permitted. Never store secrets, tokens or private transcripts in this public repo.
- The crew verifies claims independently before seeding each next prompt. Claude and Codex are collaborators, not rubber stamps. Challenge a DONE claim against the repo before confirming it. Suggest useful features where tickets leave the details open, but do not silently change the locked scope: Windows desktop coding workspace, Claude Code and Codex runtime, first-class provider/model switching, local context handoff, CLI mode retained.
- Subscription access, supported model names, native chat history and connector parity are questions to prove, not facts to claim. Do not evade provider restrictions or collect user credentials.

## Current prompt - 2 (VLI-158/159, Claude runtime)
Seeded 2026-09-27 SAST. Read current main and PR #1 before starting. This is the Claude lane only.

Jerome's scope decision at 14:55 SAST, verbatim: "thats the whole product today. - build it as your personal tool first, so thats correct". Build for Jerome's own Windows desktop use with his own installed, signed-in Claude Code and Codex subscriptions. Retain CLI mode. Do not design subscription-login distribution to other users, API-key fallback, or credential interception. The Windows probe has not been run on Jerome's PC; its results will be supplied later. Do not mark his account's entitlement or model catalog proven.

First, fix your PR #1 probe before asking Jerome to run any part of it. Independent review of head a7d0ad3 found three defects:
1. `run_turn` accepts the model's `can_use_tool` Write input unchanged on its allow path. Confine any Write to the disposable probe directory, validate the actual proposed target and reject escapes, absolute outside paths, traversal and links that escape; test the guard. A claim that only Write is enabled does not confine the path.
2. Child processes inherit `ANTHROPIC_API_KEY` from the environment. Explicitly strip API-key variables from every spawned CLI environment, including version/auth probes where relevant, and fail safely if subscription auth cannot be verified; test a planted fake key never reaches the child. No API-billed turns.
3. `summarize_result` prints the first 160 characters of raw model output as `result_text`, despite the no-account-identifiers claim. Never emit untrusted free text or account identifiers in shareable probe JSON; compute bounded booleans/enum statuses internally for checks like resume and include only allowlisted structured fields. Test sensitive text does not appear in stdout/report.
Also review the rest of its output paths for the same privacy and billing issues. Update the SAME PR #1 branch, add adversarial tests, run the full test suite, and report the new head SHA, diffs and evidence. Do not merge or deploy. Until the fixed PR has been independently checked, Jerome should run neither `python tools\claude_runtime_probe.py` nor `--turns`.

In parallel, design Claude's desktop runtime adapter for VLI-158/159 from your prompt-1 findings. Define the process lifecycle and stream-json event mapping for text deltas, tool activity, approval requests, per-turn terminal state, `is_error` versus exit code, rate-limit events and `model_refusal_fallback` visibility. Specify `initialize.models` discovery, `set_model`, interrupt, session resume, upgrade/version guard and explicit context handoff to Codex without claiming portable native sessions or consumer-chat history. Account for Windows executable discovery, local workspace trust, installed CLI authentication, no API-key billing and no hidden subscription-credit costs. Identify a small first vertical slice, interface and acceptance tests, open questions and any provider terms that constrain this personal-use design. Feed the design into VLI-158/159, without silently changing their scope. If implementation is warranted, keep it in a focused separate PR and do not merge/deploy. Reply directly under prompt 2 in this file with evidence and links; do not overwrite the prompt or Codex's lane.

## Previous prompt - 1 (VLI-157)
Seeded 2026-09-27 SAST.

FOR CLAUDE CODE:
Investigate the Claude Code side of a Windows desktop workspace driving an installed, unmodified, user-authenticated CLI. Jerome wants coding and model switching with his subscription, but we must establish the exact allowed integration path rather than assume a workaround. Use current official terms and a safe reproducible probe if available. Inventory actual models (including whether his examples Fable and Opus are exposed), streaming turns, local session resume, tool approvals, plugins/MCP/connectors, usage limits and failure behavior. Distinguish Claude Code sessions from Claude.ai consumer chats. Return a permitted architecture or a clear no-go, citations and tests; propose missing product features. Do not use API keys as an unapproved product fallback, intercept tokens, merge or deploy. Ticket: https://linear.app/vlive-projects/issue/VLI-157/prove-allowed-claude-code-codex-cli-desktop-runtime-and-available

## Claude reply - 1
Awaiting Claude Code.
