# Claude lane - Claude x Codex Desktop

This file is the Claude Code lane for this repo only. Codex uses `comms/codex/COMMS.md`. Project: https://linear.app/vlive-projects/project/claude-x-codex-desktop-10801153fadb

## Protocol
- Read current main; one live numbered prompt per lane. Reply below the matching prompt, with PR URL and head SHA if code was changed, tested evidence, unresolved questions and blockers. Do not rely on a stale copy, overwrite a concurrent prompt or mix in other products' work.
- Work on a branch and open a focused PR for code. On this Claude-x-Codex-CLI-Bridge repo only, Jerome authorizes autonomous testing and merging to main after your tests pass; do not merge red work or deploy. A research-only answer needs no PR. A COMMS-only reply may be committed to main with `comms: claude reply N [skip ci]` without awaiting a merge gate. Never store secrets, tokens or private transcripts in this public repo.
- Keep reviewing claims independently; a failed safety or billing test still blocks its code, even without a separate human merge gate. Claude and Codex are collaborators, not rubber stamps. Challenge a DONE claim against the repo before confirming it. Suggest useful features where tickets leave the details open, but do not silently change the locked scope: Windows desktop coding workspace, Claude Code and Codex runtime, first-class provider/model switching, local context handoff, CLI mode retained.
- Subscription access, supported model names, native chat history and connector parity are questions to prove, not facts to claim. Do not evade provider restrictions or collect user credentials.
- Workspace (Jerome, 2026-09-29): work only inside the repo. Branch worktrees go in `.worktrees/`, created with `python -m tools.repo_hygiene new claude/<topic>`. Never switch branches in the main checkout. After every merge, run `python -m tools.repo_hygiene clean --apply` and report its output. Full rules: `AGENTS.md`.

## Current prompt - 4 (VLI-157/159/160, Claude runtime + desktop slice)
This is a project handover. You and Codex now own day-to-day delivery for Jerome; work from VLI-157..163 as the source of scope and keep those tickets current with evidence, decisions, blockers and dependencies. Build the next Windows-first Claude runtime/UI slice that can proceed without Jerome's PC acceptance: wire the existing Claude runtime into the shared desktop coding timeline; discover and expose every Claude model currently offered by the installed CLI at runtime; include Fable, but require explicit per-session confirmation before using it; preserve native session IDs and resumability, stream events/limits/failures, and show tool/approval state. Implement the approval-mode toggle as locked in VLI-159: ask for every edit by default, with auto-accept only in folders the user explicitly trusted. Keep workspace trust explicit, CLI mode working, and no API keys, credential interception, or hidden billing. Use the existing event contract and handoff boundary; do not claim consumer-chat import or cross-provider native session transfer. In collaboration mode (VLI-163), coordinate with Codex; only present a shared final answer after both agree. Single-agent mode output stays with its chosen lane. Make code changes/tests on focused branches, merge safe passing changes autonomously to main only in this bridge repo under Jerome's existing repo-only grant; do not deploy. Keep COMMS one live numbered prompt per lane, one `## Current prompt` heading, reply below this prompt with commits/PRs, exact tests, proof, open risks and ticket links. Update and visually inspect complete COMMS file after each edit; GitHub web editor previously corrupted file structure, so re-read full file and verify exactly one current heading before commit. Keep working independently; escalate to Jerome through COMMS only for a security/policy issue, spending/credits, or genuinely contradictory requirements. Windows account/runtime acceptance remains an explicit open gate for Jerome's PC; do not claim it passed or ask for model turns unless separately authorized. Anthropic distribution/legal implications were not verified and are deferred; personal-use tool first, no distribution claim. Caution: headless Fable may consume credits without its interactive consent, so enforce per-session confirmation in host before spawn/use. Jerome's model examples illustrate intent, not exhaustive literal spec: all Claude and all OpenAI models the respective CLI exposes must be callable.

## Claude reply - 4
Replied 2026-09-28 20:55 SAST by Claude Code, running locally on Jerome's Windows 11 PC. **The Claude runtime is wired into the shared desktop timeline and merged.** No deployment. No model turn was run or requested.
- **PR:** https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/12
- **Head:** `f4fc115`, squash-merged to main as `f225c5f`. The merged tree is identical to the reviewed head.
- **Builds on:** Codex [PR #11](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/11), `338c61a`.
- **Tickets:** [VLI-158](https://linear.app/vlive-projects/issue/VLI-158), [VLI-159](https://linear.app/vlive-projects/issue/VLI-159), [VLI-160](https://linear.app/vlive-projects/issue/VLI-160), [VLI-163](https://linear.app/vlive-projects/issue/VLI-163). **I could not update Linear from this session: it has no Linear connector.** Paste-ready ticket notes are at the end of this reply.

### Delivered
- **Every model the installed CLI offers is callable.** `Preflight.models` is now the live menu, with `value`, `resolved_model`, `display_name`, `effort_levels` and `credit_billed`. Values keep their exact spelling, including `[1m]` and IDs the probe's fixed sets do not know. Before, the desktop picker received redacted names such as `unlisted-claude-opus`, which `--model` cannot accept. The shareable, redacted form is now `models_report`.
- **Fable asks before each Claude session, enforced in the adapter before spawn** (`CreditConsent`).
  - Each consent names the model the user confirmed and binds to exactly one `ClaudeSession`.
  - A resumed or respawned process asks again, and so does a mid-session `set_model`.
  - A `default` that resolves to Fable counts, whether the resolved model or only the display name says so. Matching ignores case. It is re-checked against the chat process's own live menu.
  - `default` is always pinned with `--model`. Otherwise a user or project settings file could choose the model without the check seeing it.
  - If the CLI's `system/init` reports a credit-billed model with no consent, the turn is interrupted, and the session refuses further turns.
  - The desktop clears an unused consent after every run and on disconnect.
- **The VLI-159 approval toggle works for Claude**, through the new shared `tools/approval_modes.py`. `TrustedFolderStore` moved there, `codex_app_server` re-exports it, and both providers use one trusted-folder list.
  - **Routing.** Each chat process gets `--settings` with `permissions.ask` for `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash` and `PowerShell`. So user or project allow rules cannot skip the host.
  - **Ask mode.** `ask_every_edit` is the default: every edit and command is a blocking card.
  - **Auto mode.** `auto_accept_trusted` needs the folder explicitly trusted for automatic edits. It accepts only file edits whose target resolves strictly inside that folder, with the validated path pinned.
  - **Never auto-accepted:**
    - agent config: `.git`, `.claude`, `.claude.json`, `.codex`, `.mcp.json`, `.agent-bridge`, `.vscode`, `.github`, `.husky`
    - NTFS stream spellings, trailing-dot or trailing-space spellings, and 8.3 short names
    - commands (Claude Code has no Windows command sandbox)
  - **Changes.** The mode can change only between turns. Revoking a folder's trust takes effect at the next request.
- **Billing is re-checked inside the chat process.** Its own `initialize` must report a first-party subscription with no API key source; otherwise the process is closed before any user turn. An API-key source reported mid-turn interrupts the turn.
- **Events.**
  - `approval_request` carries `policy` (`ask`, `auto_accept` or `declined_failed_turn`).
  - `approval_decision` carries `by` (`user`, `auto_trusted` or `host_failed_turn`), matching Codex's kind.
  - `runtime_error` covers assistant error codes (`auth`, `billing`, `limit`, `connection`, `other`), with no model text.
  - A `ran_without_host_approval` notice shows any edit or command that finished without reaching the host.
  - `session_ref` is a UUID assigned up front and resumed with `--resume`. It is never translated.
- **Robustness.**
  - `MIN_VERSION` is now 2.1.201. The previous floor of 2.1.280 refused Jerome's installed CLI (`cli_too_old`, confirmed live). Versions other than 2.1.283 run, labeled `older_untested` or `newer_untested`.
  - For `.cmd` shims, arguments containing `% " & | < > ^ ( ) !`, CR, LF or NUL are refused. Session IDs must be UUIDs.
  - On Windows, closing kills the whole process tree.
  - `fail_turn()` declines anything still in flight after a failure or Stop.
  - `host_timeout` interrupts and forces a new, resumed session.
  - Writes to stdin are locked.
- **Desktop host (`tools/desktop.py`, Codex's).**
  - Removed the "Claude auto-accept not wired" block.
  - Only `policy=ask` requests reach the user. Before this, an auto-accepted request would have been answered again and raised `unknown_approval_request`.
  - Claude-only auto mode now grants the shared auto-edit trust. Before, it never did.
  - A runtime error drains to the CLI's own turn end, so it cannot leak into the next turn.
  - Refusals show their fixed reason code, such as `cli_too_old`.
  - The catalog line shows CLI version status, plan and the full menu, with Fable marked "asks before use".
- **Collaboration (VLI-163)** uses Codex's gate unchanged: the result is joint only after exact approvals from both. I routed the Claude turns in it through the approval toggle. Single-provider modes keep their own answer. No consumer-chat import or native cross-provider session transfer is claimed.
- **CLI mode is untouched.** `tools/agent_bridge.py --dry-run` gives the same commands as before.

### Evidence
- **Tests:** `python -W error::ResourceWarning -m unittest discover -s tools/tests -p "test_*.py"` gives **145 tests, OK**. That run was on merged main on this Windows PC (Python 3.13.3). `python -m compileall -q tools` and `git diff --check` are clean.
  - New suites: `tools/tests/test_claude_desktop_session.py` and `tools/tests/test_desktop_claude.py`, which covers host routing without a Tk window.
  - Before this work, main had **5 Windows-only test errors**: the temp workspace was deleted before the fake CLI process that was still using it had exited. The earlier green runs were on Linux. Codex fixed the same bug in parallel.
- **Mutation check.** Each of these makes the suite fail:
  - credit gate off
  - session billing re-check off
  - auto-accept confinement off
  - live-menu re-check off
  - ask rules off
  - mid-turn mode guard off
- **Independent adversarial review** (a separate agent, two rounds).
  - **Round 1** found 3 must-fix issues:
    - Consent could be bypassed through a `default` flagged only by its display name.
    - An unpinned `default` could be steered by a settings file.
    - NTFS stream spellings such as `.mcp.json::$DATA` escaped the auto-accept confinement. Reproduced with the fake CLI.
  - It also found 4 should-fix issues:
    - `.cmd` metacharacters and unvalidated `--resume`
    - requests left unanswered after a failure
    - a host timeout that did not stop the CLI
    - a consent that could carry over to a later session
  - **Round 2** confirmed all seven fixes hold and found two more gaps: case-sensitive menu matching, and a Fable retry on every Send after `unconsented_credit_model`. Both are fixed, with regression tests.
- **Real CLI on Jerome's PC, with no model call and no user message:**
  - `python tools/claude_runtime_probe.py` (inventory only) exited 0. It reports CLI `2.1.201`, `auth_method: claude.ai`, `firstParty`, **plan `Claude Team`**, and no API key source.
  - The menu is: `default` → `claude-opus-4-8[1m]`, `opus[1m]` → `claude-opus-4-8[1m]`, `claude-fable-5[1m]` → `claude-fable-5` (Fable), `sonnet` → `claude-sonnet-5`, `haiku` → `claude-haiku-4-5-20251001`.
  - The adapter's `preflight()` accepted it as `older_untested` through the npm `.cmd` shim. The old adapter refused the same CLI with `cli_too_old`.
  - A real chat `ClaudeSession` started with the production flags: `--settings` ask rules, `--permission-prompt-tool stdio`, `--session-id` and `--model claude-opus-4-8[1m]`. It completed `initialize` in about 4 seconds, and its own reply said Team / firstParty / no API key. On close it exited 0 and the temp settings file was removed.
  - Jerome's own SessionStart hooks (the remember plugin) ran at spawn with no turn and wrote into the throwaway folder. That is live evidence for the explicit trust gate.

### Open risks and gates
- **Windows runtime acceptance is still open.** On Jerome's PC, no real turn, approval allow or deny, resume, interrupt, `taskkill /T` or UTF-8 reply has been run. It needs his separate authorization. I am not asking for it here.
- **Ask-rule precedence is unverified live.** That ask rules outrank a user's allow rules comes from the docs (https://code.claude.com/docs/en/permissions) and the fake-CLI tests. It has not been observed in a real turn. The `ran_without_host_approval` notice is the visible backstop.
- **Friction.** Every `Bash`/`PowerShell` command asks, including `git status`. A later option could allow one exact command for the session.
- **Refusals that may be too strict:**
  - A `.cmd` install whose temp path contains `( ) !` or `&` is refused.
  - Ordinary names like `file~1.txt` ask instead of auto-accepting.
  - If the live `default` changed since preflight, the session refuses and the user must reconnect. The host does not re-prompt.
- **Codex's strict mode** is still proposal-only until its per-edit staging and apply path exists. That is Codex's lane.
- **Coordination.** Both lanes ran in the same checkout at `C:\Users\choma\Desktop\Codex-Fable-Bridge`, and Codex switched branches under this session. I worked in `..\Codex-Fable-Bridge-claude`. **Codex: please use a separate worktree too.** Five stray `claude-desktop-ask-*.json` temp files came from the reviewer's repro runs; I deleted them.
- **Anthropic distribution and legal terms:** not verified, and deferred. Personal use only.

### Ticket notes (paste into Linear; this session cannot)
- **VLI-158:**
  - Claude runtime wired into the desktop timeline in PR #12 (`f225c5f`), with the live callable menu and native UUID sessions.
  - Jerome's PC: CLI 2.1.201 accepted as `older_untested`; preflight and a no-turn session were verified.
  - Open: Windows turn acceptance.
- **VLI-159:**
  - The Claude approval toggle is implemented: ask by default; auto only in folders explicitly trusted for auto edits; commands always ask. The trusted-folder list is shared with Codex.
  - Open: live check of ask-rule precedence, and Codex's per-edit apply.
- **VLI-160:**
  - The inspectable handoff boundary is unchanged, and no native transfer is claimed.
  - New `approval_decision`, `runtime_error` and `ran_without_host_approval` kinds are documented in `docs/claude-desktop-runtime.md`.
- **VLI-163:**
  - Claude turns in collaboration now honour the toggle and the per-session Fable consent.
  - The joint gate is still Codex's.

## Previous prompt - 3 (Claude probe safety follow-up)
Seeded 2026-09-27 SAST. This is the Claude lane only. Read current main, especially `tools/claude_runtime_probe.py` and `tools/tests/test_claude_runtime_probe.py`, before changing anything.

Our independent review confirms that PR #1 materially fixed the original Write confinement, raw model-result disclosure and inherited Anthropic API-key defects. But two remaining holes mean Jerome must **not run `python tools\claude_runtime_probe.py --turns`** until we re-review a corrected probe and explicitly clear that gate. The earlier reply 2 claim that the turn probe is safe is superseded by this hold.

1. `child_env` strips 12 named billing variables but still leaves `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, `GOOGLE_APPLICATION_CREDENTIALS` and other cloud-provider credential variables in all child processes. The assertion that cloud-provider variables are removed is false. Audit the full provider credential and billing environment, strip it from every spawned CLI process (including version/auth preflight), and extend the planted-key/secret tests to cover these values and verify that none reach any child. Keep subscription auth fail-closed; do not introduce an API-billed fallback.
2. `safe()` validates shapes with regexes such as `[A-Za-z0-9_.-]{1,64}` rather than using finite allowlists. A short CLI-origin private value with that shape can therefore be printed verbatim in provider/status/notice/model-ID fields. Replace this with actual finite, explicitly mapped public enums where possible and redaction or non-reversible bounded summaries where arbitrary identifiers are necessary. Audit every JSON output path, including errors. Add adversarial tests with a short alphanumeric private marker in each affected CLI-origin field; assert that it never appears in stdout/shareable report while useful non-sensitive diagnostics remain.

Fix both holes in a focused branch/PR, run adversarial tests and the full suite, self-review the diff against the no-extra-billing and no-identifier-leak claims, then merge green code to main under Jerome's **repo-only** grant. Do not deploy or change other repos. Reply directly below prompt 3 with PR/commit links, head SHA, test commands and results, exact environment categories stripped, output schema/redaction evidence, remaining uncertainty and any Windows-only checks. Do not ask Jerome for a `--turns` run; our independent re-review must pass first.

## Claude reply - 3
Replied 2026-09-27 by Claude Code (cloud session). Both holes are closed.
- **PR:** https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/5
- **Head:** `e207d14`, merged to main as `2a3837d`.
- **Gate:** I am **not** asking Jerome for a `--turns` run. The gate stays closed until your re-review clears it.

### Hole 1: environment, now a true allowlist
- `child_env()` builds each child environment **only** from `ENV_ALLOWLIST`, matching names case-insensitively for Windows. It applies to every probe child and to the adapter's `preflight()`: `--version`, `auth status`, the `initialize` process, every stream session and the unknown-model call.
- **What passes through:**
  - Windows process and profile basics: `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`, `PROGRAMFILES*`, `TEMP`, `TMP`, `USERNAME` and similar
  - the POSIX equivalents
  - locale settings
  - `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `ALL_PROXY`
  - `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`
  - `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_GIT_BASH_PATH`
- **Forced values:** `ENABLE_CLAUDEAI_MCP_SERVERS=false` and `DISABLE_AUTOUPDATER=1`.
- **Everything else is dropped.** Seven categories are documented and planted in tests (`STRIPPED_CATEGORIES`), but the allowlist is what enforces the rule:
  1. **Anthropic API:** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS` and model overrides.
  2. **Claude Code tokens and switches:** `CLAUDE_CODE_OAUTH_TOKEN` (plus its `_FILE_DESCRIPTOR` variant), `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `CLAUDE_CODE_USE_BEDROCK`, `_USE_VERTEX` and `_USE_FOUNDRY`, the matching `CLAUDE_CODE_SKIP_*_AUTH` variables, and `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`.
  3. **AWS:** access key, secret, session token, profile, region, `AWS_BEARER_TOKEN_BEDROCK`, config and credentials file paths, web identity, role ARN, and container credentials.
  4. **Google Cloud:** `GOOGLE_APPLICATION_CREDENTIALS`, project IDs, `CLOUDSDK_*`, `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID` and `VERTEX_REGION_*`.
  5. **Azure / Foundry:** `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID`, certificate and federated-token files, `ANTHROPIC_FOUNDRY_*`, and the managed-identity endpoints and headers.
  6. **Other LLM and CI tokens:** `OPENAI_API_KEY`, `GITHUB_TOKEN`, `GH_TOKEN`, `NPM_TOKEN`.
  7. **Code injection:** `NODE_OPTIONS`, `NODE_PATH`, `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`.

  Unlisted variables are also dropped, for example `SOME_FUTURE_CLOUD_SECRET`, `ARM_CLIENT_SECRET` and `GOOGLE_OAUTH_ACCESS_TOKEN` (tested).
- **Adapter chat sessions** (`tools/claude_runtime.py`) use `session_env()`. A full allowlist would break Jerome's real coding sessions by dropping `JAVA_HOME` and his own tool credentials. Instead `session_env()` removes every Claude billing route:
  - prefixes `ANTHROPIC_`, `CLAUDE_CODE_USE_`, `CLAUDE_CODE_SKIP_`, `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_API_KEY`
  - `AWS_BEARER_TOKEN_BEDROCK`, `VERTEX_REGION_*`, `CLOUD_ML_REGION` and `NODE_OPTIONS`

  General cloud credentials stay available to his own tools, but they cannot route Claude billing without those switches. Preflight still refuses anything other than a first-party `claude.ai` login, which catches switches set through settings files.
- **Fail-closed gate.** Turns now also require `authMethod == "claude.ai"` and no `apiKeySource` in either `auth status` or `initialize`. This is on top of logged in, exit 0, `firstParty` and a Pro/Max/Team/Enterprise plan. There is no API-billed fallback.
- **Probe models** are limited by argparse to `haiku`, `sonnet`, `claude-haiku-4-5` and `claude-sonnet-5`.

### Hole 2: output, now finite public enums
- The regex shape check `safe()` is gone. `enum()` echoes a value only if it is **exactly** a member of a fixed public set; anything else prints as `unlisted`. No hashing and no truncation, so a short private value is never recoverable.
- **Fixed sets:**
  - auth method: `claude.ai`, `oauth_token`, `api_key`, `api_key_helper`, `third_party`, `none`. These were extracted from the CLI 2.1.283 binary.
  - provider: `firstParty`, `bedrock`, `vertex`, `foundry`
  - plan: Claude Pro, Max, Team, Enterprise and API
  - permission mode, effort level, rate-limit status, rate-limit type and window name
  - notice subtype and trigger
  - result subtype and `terminal_reason` (values seen in the binary)
- **Model IDs and aliases:** known public IDs and aliases, with the `[1m]` suffix, pass through. Others become a family bucket such as `unlisted-claude-opus` or plain `unlisted`, and keep no part of the original string. Display names come from a fixed set.
- **Numbers:** utilization is 0–10, rounded to 0.01. Reset times must fall within the 2020–2096 epoch range. HTTP status must be 100–599. Exit codes must be within ±255. Counts are capped. Unknown window names are counted in `unlisted_windows`, never used as keys.
- **Output paths audited:** version (a digits-only semver match), auth, account, models, permission mode, commands count, every turn summary (deltas and approval counters, rate limit, notices, result), resume (`recalled` boolean), approvals (`file_written`), interrupt, unknown model, `turns_refused` (internal enum) and **errors**.
  - `main()` now catches everything and prints only `{"error": "claude_not_found" | "cli_timeout" | "cli_spawn_failed" | "internal_error"}` with exit 3: no exception text, paths or CLI output.
  - Child stderr is never read into output.
  - The fields that `auth status` also returns (`email`, `orgId`, `orgName`, `projectsDirectory`, `configDirectory`) are never read.

### Tests and evidence
- **Command:** `python -m unittest discover -s tools/tests -p "test_*.py"` gives **93 tests, OK**, also under `-W error::ResourceWarning`.
- **Planted marker.** `tools/tests/fake_claude_cli.py` plants `priv8x7q`, a short lowercase alphanumeric value that would have passed every old regex. It runs in two modes:
  - `FAKE_MARKER=all`: every CLI-origin field is marked, gating ones included. The marker is absent from stdout and stderr, the gate fails closed (`not_a_claude_ai_login`) with **zero** user turns, and the version is still reported.
  - `FAKE_MARKER=turns`: every non-gating field is marked, including rate-limit status, type, overage status and a window key; the notice's models and trigger; result subtype, `terminal_reason` and `modelUsage` keys; menu value, resolved model, display name and effort; and permission mode. The marker is absent, and the diagnostics survive: `ok=true`, known models, `unlisted-claude-opus`, utilization, the `unlisted_windows` count and the `model_refusal_fallback` subtype.
- **Planted credentials.** A credential is planted in every stripped category, plus lowercase and unlisted names. The fake logs each child's environment variable names and which variables held planted values. The assertion: none reached any of the 8 or more probe children, or the adapter's preflight children. Adapter chat sessions never see a billing-route variable.
- **Errors:** an exception that carries secret text plus the marker prints only `{"error": "internal_error"}`.
- **Mutation check.** Each of these makes the suite fail:
  - the old prefix denylist env: 3 failures
  - regex-shape enums: 19
  - model-ID passthrough: 4
  - traceback in the error output: 1
  - echoing window keys: 2
  - turning off the `claude.ai` gate: 3
- **Live run in this container** (CLI 2.1.283, "Claude API" login, planted `AWS_SECRET_ACCESS_KEY` and `GOOGLE_APPLICATION_CREDENTIALS`): exit 2, `turns_refused: not_a_claude_ai_login`, and 0 occurrences of anything planted in the report. The real model menu still printed public names: default/sonnet → `claude-sonnet-5`, `claude-fable-5-1`, opus → `claude-opus-5-5`, haiku → `claude-haiku-4-5-20251001`.

### Remaining uncertainty and Windows-only checks
- **Enum sets are tied to CLI 2.1.283.** A newer CLI's new plan label, auth method, model or `terminal_reason` will print as `unlisted`. That is safe, but less informative until the sets are updated. The version guard flags newer CLIs.
- **Windows login location.** I have assumed a signed-in Windows CLI needs only allowlisted variables to find its login: `USERPROFILE` or `CLAUDE_CONFIG_DIR` for `.credentials.json`, plus `APPDATA` and `LOCALAPPDATA`. If a Windows install needs another variable, auth will fail closed (`not_logged_in` or `not_a_claude_ai_login`) rather than bill anything. The inventory-only probe (no `--turns`) is the safe first check of this, once you clear it.
- **Not testable here:** `.exe` vs `.cmd` spawning under the allowlist, and whether case-insensitive matching behaves as intended on real Windows (e.g. `Path` vs `PATH`). The logic is unit-tested with mixed-case names. Settings-file `env` blocks and managed policy settings are outside the environment; the `claude.ai` and `firstParty` gate covers them.
- **Housekeeping:** the stray branch `claude/desktop-runtime-adapter` still needs deleting by someone with delete rights.


### Addendum 2026-09-28: re-check after Codex PR #7
- **Prompt 3 is unchanged**, and reply 3 above still stands. I re-ran the suite on main at `4ad6f0b`: the probe's environment and output code (`tools/claude_runtime_probe.py`) was untouched by [PR #7](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/7), and every prompt-3 adversarial test still passes.
- **PR #7 did introduce one regression in the Claude adapter:**
  - `tools/claude_runtime.py` began importing `tools.runtime_events` while its other import stayed flat, so it only imported with both the repo root and `tools/` on `sys.path`.
  - From any other directory, or imported as `tools.claude_runtime`, it raised `ModuleNotFoundError`, and the Claude suites failed when run from outside the repo.
  - **Fixed in** https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/8 (head `6228928`, merged to main as `7a4ac7b`). Both import styles now work. `Envelope` comes from the package first, so both adapters share one class, and a subprocess test covers each style.
- **Tests:** `python -m unittest discover -s tools/tests -p "test_*.py"` gives **103 tests, OK**. The Claude suites run from `/tmp` also pass.
- **The `--turns` gate is still yours to clear.** I am not asking Jerome to run it.

### Inventory-only probe run, 2026-09-28 (cleared run; no `--turns`)
Command, from the repo root on main at `39c2173`: `python3 tools/claude_runtime_probe.py` (this Linux container's equivalent of `python tools\claude_runtime_probe.py`). CLI `2.1.283 (Claude Code)`. **Exit 0, empty stderr, no user turn and no model call.** `--turns` was not run and is not being requested.

**Full report, verbatim stdout:**
```json
{
  "version": "2.1.283",
  "auth": {
    "logged_in": true,
    "auth_method": "oauth_token",
    "api_provider": "firstParty",
    "api_key_source_present": false,
    "exit_code": 0
  },
  "account": {
    "plan": "Claude API",
    "api_provider": "firstParty",
    "api_key_source_present": false
  },
  "models": [
    {
      "value": "default",
      "resolved_model": "claude-sonnet-5",
      "display_name": "Default (recommended)",
      "effort_levels": [
        "high",
        "low",
        "max",
        "medium",
        "xhigh"
      ]
    },
    {
      "value": "sonnet",
      "resolved_model": "claude-sonnet-5",
      "display_name": "Sonnet",
      "effort_levels": [
        "high",
        "low",
        "max",
        "medium",
        "xhigh"
      ]
    },
    {
      "value": "claude-fable-5-1",
      "resolved_model": "claude-fable-5-1",
      "display_name": "Fable",
      "effort_levels": [
        "high",
        "low",
        "max",
        "medium",
        "xhigh"
      ]
    },
    {
      "value": "opus",
      "resolved_model": "claude-opus-5-5",
      "display_name": "Opus",
      "effort_levels": [
        "high",
        "low",
        "max",
        "medium",
        "xhigh"
      ]
    },
    {
      "value": "haiku",
      "resolved_model": "claude-haiku-4-5-20251001",
      "display_name": "Haiku",
      "effort_levels": []
    }
  ],
  "permission_mode": "default",
  "commands": 51
}
```

**What it shows:**
- **Auth.** Logged in, `auth_method: oauth_token`, `firstParty`, no API key source. This container is signed in with a host-provided token, **not** a `claude.ai` subscription login. Had `--turns` been passed, the gate would have refused with `not_a_claude_ai_login` (and, failing that, `not_a_subscription_plan` for `plan: Claude API`). None of this is evidence about Jerome's plan or his model menu, which remain **unproven**.
- **Model menu at the time of the run:** Default (→ `claude-sonnet-5`), Sonnet, Fable (`claude-fable-5-1`), Opus (`claude-opus-5-5`) and Haiku (`claude-haiku-4-5-20251001`, with no effort levels).
- **Other fields:** permission mode `default`, 51 commands.

**The menu changed on the server side between runs.** Minutes later, every run returned an 11-entry menu. It happened with and without the name-logging shim below, so the shim is not the cause. The default moved to Opus 5.5 and the display names became versioned ("Opus 5.5", "Fable 5.1", …). That later report, summarised:

| value | resolved_model | display_name | effort levels |
|---|---|---|---|
| `default` | `claude-opus-5-5` | Default (recommended) | high, low, max, medium, xhigh |
| `opus` | `claude-opus-5-5` | unlisted | high, low, max, medium, xhigh |
| `claude-fable-5-1` | `claude-fable-5-1` | unlisted | high, low, max, medium, xhigh |
| `sonnet` | `claude-sonnet-5` | unlisted | high, low, max, medium, xhigh |
| `haiku` | `claude-haiku-4-5-20251001` | unlisted | — |
| `unlisted-claude-opus` | `unlisted-claude-opus` | unlisted | high, low, max, medium, xhigh |
| `claude-fable-5` | `claude-fable-5` | unlisted | high, low, max, medium, xhigh |
| `claude-opus-4-8` | `claude-opus-4-8` | unlisted | high, low, max, medium, xhigh |
| `claude-opus-4-7` | `claude-opus-4-7` | unlisted | high, low, max, medium, xhigh |
| `claude-opus-4-6` | `claude-opus-4-6` | unlisted | high, low, max, medium |
| `claude-sonnet-4-6` | `claude-sonnet-4-6` | unlisted | high, low, max, medium |

### Remaining holes against the prompt-3 fixes
- **No leak and no billing hole found.** The redaction behaved exactly as designed on real, unplanned data:
  - Versioned display names that are not in the fixed set print as `unlisted`.
  - `claude-opus-5`, which is not in the known-ID set, prints only as the bucket `unlisted-claude-opus`.
  - Nothing from the raw values appears.
- **One diagnostic gap, not a safety hole:**
  - The finite sets are already stale against a live menu change: the versioned display names and `claude-opus-5`.
  - A later Windows inventory run would therefore show those as `unlisted`.
  - Proposed follow-up: add the public names "Opus 5.5", "Opus 5", "Fable 5.1", "Fable 5", "Sonnet 5", "Haiku 4.5", "Opus 4.8", "Opus 4.7", "Opus 4.6" and "Sonnet 4.6", plus the ID `claude-opus-5`, to the fixed sets.
  - I have **not** changed code in this run; say if you want it.
- **Menu drift.** The model menu is not a stable snapshot: it changed within minutes on the same CLI version. Probe results should always carry their timestamp, and the adapter must rebuild the menu from `initialize` on every preflight, as designed, never from a cached copy.

### Cloud-credential scrubbing confirmed on this environment
- **Parent environment:** 146 variables. 23 names look like credentials, including:
  - a real AWS access-key pair (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
  - `CLOUDSDK_AUTH_ACCESS_TOKEN`
  - `GH_TOKEN` and `GITHUB_TOKEN`
  - `ANTHROPIC_BASE_URL`
  - host session and messaging tokens
  - the code-injection hooks `NODE_OPTIONS`, `JAVA_TOOL_OPTIONS` and `BUN_OPTIONS`
- **Method.** I re-ran the same inventory with `--claude-command` pointing at a shell shim. The shim records **only the variable names** each child receives (never values) and then `exec`s the real, unmodified `/opt/claude-code/bin/claude`.
- **Result:**
  - The probe spawned 3 children: `--version`, `auth status --json`, and the stream-json `initialize` session. Each received the same 15 names: `PATH`, `HOME`, `SHELL`, `TERM`, `LC_CTYPE`, `HTTPS_PROXY`, `https_proxy`, `NO_PROXY`, `no_proxy`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, the forced `ENABLE_CLAUDEAI_MCP_SERVERS` and `DISABLE_AUTOUPDATER`, and `PWD` (set by `/bin/sh` in the shim itself).
  - **Credential-like names reaching any child: 0 of 23.** Parent variables dropped: 133 of 146.
  - All 19 credential values of 6 or more characters were checked in memory (never printed) against both reports: **0 found**. Neither report contains `@`, `org`, `uuid`, `/root` or `/home`.
- Two of the dropped names, `CLOUDSDK_AUTH_ACCESS_TOKEN` and `BUN_OPTIONS`, are not in the documented `STRIPPED_CATEGORIES` list. The allowlist removed them anyway, which is the point of switching from a denylist. `BUN_OPTIONS` matters because the native `claude` build is Bun-based.
- **Scope of this confirmation:** it covers the probe and preflight path (`child_env`) only. Adapter chat sessions use `session_env` by design, which keeps the user's own AWS, GitHub and similar credentials for his tools and strips only Claude billing routes; they were not exercised here.

## Previous prompt - 2 (VLI-158/159, Claude runtime)
Seeded 2026-09-27 SAST. Read current main and PR #1 before starting. This is the Claude lane only.

Jerome's scope decision at 14:55 SAST, verbatim: "thats the whole product today. - build it as your personal tool first, so thats correct". Build for Jerome's own Windows desktop use with his own installed, signed-in Claude Code and Codex subscriptions. Retain CLI mode. Do not design subscription-login distribution to other users, API-key fallback, or credential interception. The Windows probe has not been run on Jerome's PC; its results will be supplied later. Do not mark his account's entitlement or model catalog proven.

First, fix your PR #1 probe before asking Jerome to run any part of it. Independent review of head a7d0ad3 found three defects:
1. `run_turn` accepts the model's `can_use_tool` Write input unchanged on its allow path. Confine any Write to the disposable probe directory, validate the actual proposed target and reject escapes, absolute outside paths, traversal and links that escape; test the guard. A claim that only Write is enabled does not confine the path.
2. Child processes inherit `ANTHROPIC_API_KEY` from the environment. Explicitly strip API-key variables from every spawned CLI environment, including version/auth probes where relevant, and fail safely if subscription auth cannot be verified; test a planted fake key never reaches the child. No API-billed turns.
3. `summarize_result` prints the first 160 characters of raw model output as `result_text`, despite the no-account-identifiers claim. Never emit untrusted free text or account identifiers in shareable probe JSON; compute bounded booleans/enum statuses internally for checks like resume and include only allowlisted structured fields. Test sensitive text does not appear in stdout/report.
Also review the rest of its output paths for the same privacy and billing issues. Update the SAME PR #1 branch, add adversarial tests, run the full test suite, and report the new head SHA, diffs and evidence. Merge PR #1 yourself only after all three defects and the broader billing/output review are fixed, adversarial tests and full suite pass; do not deploy. Until the fixed PR is verified green, Jerome should run neither `python tools\claude_runtime_probe.py` nor `--turns`.

In parallel, design Claude's desktop runtime adapter for VLI-158/159 from your prompt-1 findings. Define the process lifecycle and stream-json event mapping for text deltas, tool activity, approval requests, per-turn terminal state, `is_error` versus exit code, rate-limit events and `model_refusal_fallback` visibility. Specify `initialize.models` discovery, `set_model`, interrupt, session resume, upgrade/version guard and explicit context handoff to Codex without claiming portable native sessions or consumer-chat history. Account for Windows executable discovery, local workspace trust, installed CLI authentication, no API-key billing and no hidden subscription-credit costs. Identify a small first vertical slice, interface and acceptance tests, open questions and any provider terms that constrain this personal-use design. Feed the design into VLI-158/159, without silently changing their scope. If implementation is warranted, keep it in a focused separate PR and test and merge safe code autonomously in this repo only; do not deploy. Reply directly under prompt 2 in this file with evidence and links; do not overwrite the prompt or Codex's lane.

## Claude reply - 2
Replied 2026-09-27 by Claude Code (cloud session).

### 1. PR #1 probe repair: fixed and merged
- **PR:** https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/1
- **Heads:** fixed head `d1af32d`, repair commit `b22e4bd`. Merged to main as `61f36a5`.
- **Defect 1, Write confinement.** A Write is approved only if its resolved target is inside the probe directory, and the approved input is pinned to that validated absolute path. Denied:
  - `..` in any spelling, including `sub/../x` and backslashes
  - absolute paths outside the directory
  - symlinks or junctions that escape
  - the directory itself, directory targets, NUL bytes and non-string input
  - every tool other than Write
- **Defect 2, API keys.** Every child process has these removed, case-insensitively: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, the Bedrock, Vertex and Foundry variables, and `AWS_BEARER_TOKEN_BEDROCK`. The version and auth checks are included.
  - Model turns run only if all of these hold: `auth status` exits 0 and reports logged in, the provider is `firstParty`, the `initialize` plan is Claude Pro, Max, Team or Enterprise, and no `apiKeySource` is reported.
  - Otherwise the probe exits 2 with `turns_refused: <reason>` before any model call.
- **Defect 3, report text.** `result_text` is removed. Resume is checked internally and reported only as a `recalled` boolean. Every reported field is allowlisted and pattern-checked. Model output, refusal explanations, model descriptions and account e-mail, organization or IDs cannot reach stdout.
- **Broader billing and output review (also fixed):**
  - Fable is refused as a probe model, because in `-p` mode it can bill usage credits without asking.
  - Probe sessions load no user or project settings, hooks or MCP servers (`--setting-sources ""`, `--strict-mcp-config`), and claude.ai connectors are disabled.
  - `--max-turns 2` and `--effort low` are set.
  - The version is reduced to a semver match.
  - Child stderr is never printed.
- **Evidence:**
  - 67 tests OK at that head. The new adversarial tests include end-to-end runs against `tools/tests/fake_claude_cli.py`, which never contacts a model and plants secrets and an API key. The runs show the key reached no child, no secret appeared in stdout, a "Claude API" plan was refused with zero user turns, and an escaping Write proposed by the CLI was denied and never written.
  - Mutation check: putting each defect back makes the suite fail (guard off: 5 failures; env strip off: 2; `result_text` back: 3; gate off: 1 error).
  - Live run in this container, which uses a "Claude API" login: `--turns` exits 2 with `turns_refused: not_a_subscription_plan` and makes no model call.
- **The probe is safe for Jerome to run now:** `python tools\claude_runtime_probe.py`, then add `--turns`. On a non-subscription login it will refuse turns by design. His entitlement and model menu remain **unproven** until he runs it.

### 2. Desktop runtime adapter design and first slice: merged
- **PR:** https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/2
- **Heads:** head `69b6812`, merged to main as `a07bc00`.
- **Design:** `docs/claude-desktop-runtime.md`.
- **Slice:** `tools/claude_runtime.py`.
- **Tests:** 82 OK. The 15 adapter acceptance tests run against the fake CLI.
- **Lifecycle.** Discover the CLI (PATH, then `%USERPROFILE%\.local\bin\claude.exe`, then `%APPDATA%\npm\claude.cmd`). Run preflight with no model call:
  - version guard: min 2.1.280, tested 2.1.283, with "newer untested" flagged
  - `auth status` exit code
  - the `initialize` plan and API-key-source gate
  - the model menu
  - a 60s watchdog
  Then the app's own workspace trust gate, because `-p` skips the CLI's trust dialog and runs repo hooks and `.mcp.json`. Then one long-lived process per chat: `--print` with stream-json in and out, `--include-partial-messages`, `--permission-prompt-tool stdio`, `--permission-mode default`, `--model`, and `--session-id` or `--resume`, with the environment stripped. It never uses `--bare`.
- **Provider-neutral envelope** `(provider, session_ref, kind, data)` with kinds `text_delta`, `thinking_delta`, `tool_started`, `tool_finished`, `approval_request`, `rate_limit`, `notice` (for `model_refusal_fallback` and `api_retry`), `session_started`, `turn_finished` and `process_exited`.
  - The turn verdict comes from `is_error`, never from the exit code or `subtype`.
  - Unknown event types are dropped.
  - An approval allows exactly the input the user saw, and IDs the CLI never issued are refused.
- **Models.** The menu comes only from `initialize.models`. `set_model` switches between turns. Fable is refused at spawn and on `set_model` unless the UI passes an explicit per-session confirmation, so there are no hidden credit costs.
- **Interrupt** ends the turn with `ok=false` and `aborted_streaming`.
- **Resume and history.** Resume uses the native `--resume` or `--fork-session`. The app keeps its own envelope log for history. There is no consumer-chat import.
- **Handoff to Codex.** `build_handoff()` makes a bounded, user-visible and editable packet: the user summary, selected files, tools used, the last verdict and the reply tail. It states that the Claude session is "not resumable in Codex", and no native session transfer is claimed.
- **Terms that constrain the design** (legal-and-compliance page, Agent SDK note): one end user on the unmodified binary with his own subscription, and ordinary individual usage. No credential collection, reading, storing or `setup-token`. Login only through `claude auth login` in his own terminal.

### Feeding VLI-158/159
- The design doc is written as input to VLI-158/159 without changing their scope. The UI shell technology is left open.
- **Codex lane:** please map your app-server items and approvals onto the envelope kinds above, or propose changes, so we share one schema. Native thread and session IDs stay per provider.

### Open questions / blockers
1. **Jerome:** run the fixed probe on Windows and paste its JSON. It is safe to share by construction. This proves the plan, the model menu, `.exe`/`.cmd` spawning and UTF-8.
2. **Jerome:** Fable in the desktop app, yes or no? If yes, should confirmation be per session or per turn?
3. **Default approval policy:** `default` (ask for everything, used in the slice) or `acceptEdits` inside trusted workspaces?
4. **Housekeeping:** I accidentally pushed a duplicate branch, `claude/desktop-runtime-adapter`. It is identical to PR #2 and already merged. This session's git proxy blocks remote branch deletes, so please delete it.

## Archive prompt - 1 (VLI-157)
Seeded 2026-09-27 SAST.

FOR CLAUDE CODE:
Investigate the Claude Code side of a Windows desktop workspace driving an installed, unmodified, user-authenticated CLI. Jerome wants coding and model switching with his subscription, but we must establish the exact allowed integration path rather than assume a workaround. Use current official terms and a safe reproducible probe if available. Inventory actual models (including whether his examples Fable and Opus are exposed), streaming turns, local session resume, tool approvals, plugins/MCP/connectors, usage limits and failure behavior. Distinguish Claude Code sessions from Claude.ai consumer chats. Return a permitted architecture or a clear no-go, citations and tests; propose missing product features. Do not use API keys as an unapproved product fallback, intercept tokens, merge or deploy. Ticket: https://linear.app/vlive-projects/issue/VLI-157/prove-allowed-claude-code-codex-cli-desktop-runtime-and-available

## Claude reply - 1
Replied 2026-09-27 by Claude Code (cloud session). PR: https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/1. The probe code is at `c47338e`; the PR head is the commit that adds this reply.

> **Superseded 2026-09-27:** do not run the probe at `c47338e`/`a7d0ad3`. Review found three defects: unconfined Write approvals, an inherited `ANTHROPIC_API_KEY`, and model text in the report. See Claude reply 2 for the fixed head.

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
