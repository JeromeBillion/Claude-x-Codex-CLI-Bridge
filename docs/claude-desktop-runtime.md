# Claude Code desktop runtime adapter (VLI-158/159)

Status: adapter slice (`tools/claude_runtime.py`), wired into the shared desktop
host (`tools/desktop.py`). It is tested against a fake CLI, and CLI 2.1.283 was
observed in a Linux container. On Jerome's Windows PC (2026-09-28) the adapter's
**preflight only** has run, with no model call. It found CLI 2.1.201 through the
npm `.cmd` shim, a `claude.ai` login on Claude Team, `firstParty`, no API key
source, and a five-entry menu that includes Fable. **No turn, approval, resume
or interrupt has been exercised on his PC.** Windows runtime acceptance remains
open.

Scope is Jerome's own Windows desktop. His own installed, signed-in Claude
Code and Codex CLIs drive it, and CLI mode (`bridge.ps1`) stays. Out of scope:
distributing subscription login to others, API-key fallback, and intercepting
credentials.

## Provider terms that constrain this design

From https://code.claude.com/docs/en/legal-and-compliance and
https://code.claude.com/docs/en/agent-sdk/overview:

- **Allowed:** "an end user signing in to the unmodified Claude Code binary with their own Claude subscription".
- **Assumed usage level:** plan limits "assume ordinary, individual usage of Claude Code and the Agent SDK". The adapter never multiplies sessions to go past that.
- **Credentials:** developers "may not collect, store, or intermediate Claude.ai credentials or session tokens". The adapter never reads `.credentials.json`, never runs `setup-token`, and never sets `CLAUDE_CODE_OAUTH_TOKEN`. Login happens only through `claude auth login` in the user's own terminal.
- **Other users:** third parties may not "offer Claude.ai login" or "route requests through Free, Pro, or Max plan credentials on behalf of their users". This is why distribution stays out of scope unless Anthropic approves it in writing.
- **Unmodified binary:** the binary must not be modified. The adapter spawns the user's `claude` exactly as installed.

## Process lifecycle

```
discover → preflight → trust gate → spawn chat process → turns … → close
                                         ↑ resume (--resume <id>) after restart
```

1. **Discover.** Search `PATH` first. Then try `%USERPROFILE%\.local\bin\claude.exe` (native installer), then `%APPDATA%\npm\claude.cmd` (npm shim). If none is found, return `claude_not_installed`, and the UI links to the official install page. Never bundle a binary or use the Agent SDK's bundled one.
2. **Preflight** makes no model call:
   - **Version.** Parse `claude --version`.
     - Below `MIN_VERSION` (2.1.201) the adapter refuses with `cli_too_old`. 2.1.201 is the oldest CLI seen with every flag the adapter passes and a model menu in `initialize`: Jerome's install.
     - Exactly `TESTED_VERSION` (2.1.283), whose turn shapes were observed, is `tested`. Anything else runs, labeled `older_untested` or `newer_untested`.
     - The earlier floor of 2.1.280 would have refused Jerome's installed CLI outright.
     - Re-run the probe after every CLI update.
   - **Auth.** `claude auth status --json` must exit 0 and report `loggedIn`.
   - **Plan.** Start a stream-json process with no tools and send `control_request` `initialize`. `account.subscriptionType` must be Claude Pro, Max, Team or Enterprise. `apiProvider` must be `firstParty` and no `apiKeySource` may be present. Otherwise the adapter refuses (`not_a_subscription_plan`, `not_first_party` or `api_key_source_present`). "Claude API" is refused.
   - The same response gives `models[]`, the model menu.
3. **Trust gate.** `-p` skips the CLI's workspace trust dialog and runs that repo's hooks and `.mcp.json` servers without asking (headless docs). The app therefore keeps its own `TrustStore`. The first session in a folder requires an explicit "Trust this folder" click, and the dialog lists `.claude/settings*.json` hooks and `.mcp.json` servers.
4. **Spawn.** Run one long-lived process per chat, with `cwd` set to the workspace and the child environment passed through `child_env()`:
   ```
   claude --print --input-format stream-json --output-format stream-json --verbose
          --include-partial-messages --permission-prompt-tool stdio --permission-mode default
          --settings <temp ask-rules file> [--model <menu value>] [--effort <level>]
          (--session-id <uuid> | --resume <id>)
   ```
   - **Model flag.** `--model` is omitted for `default`, so the CLI keeps its own default. Every new chat gets a `--session-id` up front, so its native ID is known before the first event.
   - **Initialize.** The adapter sends `initialize` in the chat process, as the Agent SDK does. It then re-checks that reply's account (first party, subscription plan, no API key source) and refreshes the menu from it. A mismatch closes the process before any user turn.
   - **`.cmd` shims.** Through an npm `.cmd` shim, any argument containing `%`, `"`, CR, LF or NUL is refused (`unsafe_cmd_argument`), because `cmd.exe` would rewrite it.
   - Environment, in two tiers:
     - Preflight (and the probe) run with `child_env()`, a strict allowlist of OS, profile, locale, proxy/CA and `CLAUDE_CONFIG_DIR` variables.
     - Chat sessions run with `session_env()`. It keeps the user's development environment but removes every `ANTHROPIC_*`, `CLAUDE_CODE_USE_*`, `CLAUDE_CODE_SKIP_*`, `CLAUDE_CODE_OAUTH_TOKEN*` and `CLAUDE_CODE_API_KEY*` variable, plus `AWS_BEARER_TOKEN_BEDROCK`, `VERTEX_REGION_*`, `CLOUD_ML_REGION` and `NODE_OPTIONS`. General AWS, Google Cloud and Azure credentials stay available to the user's own tools, but they cannot route Claude billing without the stripped provider switches.
     - Preflight also refuses anything that is not a first-party `claude.ai` login, which catches provider switches set through settings files.
   - Never pass `--bare`, which ignores OAuth. The docs say bare mode will become the default for `-p`, and the version guard exists to catch that.
   - User and project settings, plugins and MCP servers load as normal. That is the point of a personal workspace, and the trust gate covers it.
5. **Close.** Close stdin, wait, then kill if needed. On Windows, kill with `taskkill /T` for `.cmd` shims, as the bridge already does. SIGTERM or a kill mid-turn leaves the turn unfinished, so the UI sends `interrupt` first.

## Provider-neutral envelope

`Envelope(provider, session_ref, kind, data)` lives in
`tools/runtime_events.py` and is imported by both adapters. `session_ref` stays
each provider's native ID and is never translated.

| stream-json input | envelope kind | data | UI |
|---|---|---|---|
| `stream_event` → `content_block_delta` / `text_delta` | `text_delta` | `text` | append to reply |
| … / `thinking_delta` | `thinking_delta` | — | "thinking" indicator (content not shown) |
| `assistant` message `tool_use` block | `tool_started` | `tool`, `tool_use_id` | activity row |
| `user` message `tool_result` block | `tool_finished` | `tool_use_id`, `is_error` | close activity row |
| `control_request` `can_use_tool` | `approval_request` | `request_id`, `tool`, `input` | blocking approval card |
| `rate_limit_event` | `rate_limit` | `status` (allowed / allowed_warning / rejected), `type`, window utilization and reset | usage meter; banner on warning or rejected |
| `system` `model_refusal_fallback` / `api_retry` | `notice` | `original_model`, `fallback_model`, `trigger` | inline notice: "Fable stopped; continued on Opus 4.8" |
| `system` `init` | `session_started` | `model`, `permission_mode`, `api_key_source` | header; alarm if `api_key_source` ≠ none |
| `result` | `turn_finished` | `ok` (= `is_error is False`), `subtype`, `terminal_reason`, `api_error_status`, `permission_denials`, `models_served` | end of turn |
| host answer (user or trusted-folder auto) | `approval_decision` | `request_id`, `decision` (`accept` / `decline`), `by` (`user` / `auto_trusted`) | resolves the approval card |
| `assistant` message with `error` | `runtime_error` | `category` (`auth` / `billing` / `limit` / `connection` / `other`), `code` from a fixed set | banner; turn fails |
| host-decided tool finished with no host approval | `notice` | `subtype: ran_without_host_approval`, `tool` | warning: an ask rule was bypassed |
| process EOF | `process_exited` | `exit_code` | "runtime stopped", with a resume button |

Codex mapping added by the Codex lane (VLI-158/159):

| App Server event | shared kind | data / UI |
|---|---|---|
| `item/agentMessage/delta` | `text_delta` | `text`; append |
| reasoning delta or summary boundary | `thinking_delta` | content omitted; indicator only |
| command, file, MCP, web or collaboration item `started` / `completed` | `tool_started` / `tool_finished` | `tool`, native `tool_use_id`, `is_error` |
| command or file `requestApproval` | `approval_request` | `request_id`, `family`, command/cwd/reason when supplied; blocking |
| host answer to an issued command/file approval | `approval_decision` | `request_id`, `decision`; host-side confirmation |
| `turn/completed` | `turn_finished` | `ok`, `subtype`, `terminal_reason`, `api_error_status` |
| `account/rateLimits/updated` | `rate_limit` | bucket, use percentage, reset timestamp |
| `thread/started` | `session_started` | native Codex thread ID is `session_ref` |
| `host/disconnected` | `process_exited` | exit code; offer resume |
| warnings, reroute, plan update | `notice` | subtype |
| `account/updated` | `account_status` | auth mode, plan category; never email/account ID |
| upstream `error` or protocol failure | `runtime_error` | category only (`auth`, `limit`, `connection`, `other`) |
| MCP elicitation / connector user-input request | `mcp_elicitation` / `connector_approval_request` | blocking; dedicated UI required |

The last four kinds extend the original Claude table where Codex has a
different observable control surface. Claude's lane can adopt them when it
adds comparable account, runtime and connector events. Unknown App Server
events are dropped. A Codex request outside the implemented command/file
approval family stays blocked or causes interruption; the host never guesses
an approval payload. Codex account and error envelopes omit raw service text.

Rules:
- **Turn verdict.** It comes from `is_error`, never from the exit code or `subtype`. An unknown model was observed returning `subtype: "success"`, `is_error: true`, 404 and exit code 1.
- **Unknown event types** are dropped, not treated as errors, so a CLI upgrade can add events without breaking the UI.
- **Approval answers.** Allow sends exactly the input the user saw, or the version the user edited. Deny sends a message. An approval ID the CLI never issued is refused.

## Models, switching, interrupt, resume

- **Menu.** Always build it from `initialize.models` (`value`, `resolvedModel`, `displayName`, effort levels). Never hard-code names. Fable, Opus, Sonnet and Haiku appeared in the container's menu; Jerome's plan menu is **unproven**.
- **`set_model`.** A `control_request` `set_model` switches the model mid-session; this was observed switching Haiku to Sonnet. It is sent between turns.
- **Menu passthrough.** `Preflight.models` keeps every offered value exactly, including `[1m]` suffixes and IDs the probe's fixed sets do not know. The redacted names (`unlisted-claude-opus`) could never be passed back to `--model`. Values that are unsafe on a command line are dropped.
- **Hidden credit costs.** In `-p`, Fable "bills it without asking" when a request would draw on usage credits.
  - The adapter refuses a credit-billed model before spawning, and on `set_model`, unless the UI passes a `CreditConsent` made after an explicit confirmation. A credit-billed model is one whose value, resolved model or display name matches Fable or `best`; that includes a `default` that resolves to Fable.
  - A consent binds to one `ClaudeSession`. Reusing it in another session, a resumed process included, is refused (`credit_consent_already_used`).
- **Interrupt.** A `control_request` `interrupt` ends the turn with `turn_finished` `ok=false`, `terminal_reason: aborted_streaming`.
- **Resume.** New chats get `--session-id <uuid>` and reopened chats get `--resume <uuid>`; `--fork-session` branches a chat.
  - The CLI owns the transcript (`%USERPROFILE%\.claude\projects\…\<id>.jsonl`). Its format is internal and it is pruned after 30 days.
  - The app keeps its own envelope log for rendering history, and treats the session ID only as a resume handle.
- **Consumer chats.** There is no import from claude.ai consumer chats. No supported interface exists for it, and the app does not claim one.

## Approval toggle (VLI-159, Jerome's locked decision)

**Ask for every edit** is the default. **Auto-accept in trusted folders** needs
a separate, explicit trust grant for that exact folder (`TrustedFolderStore`,
shared with Codex). It can change only between turns.

- **Routing.** Every chat process gets `--settings` with `permissions.ask` for
  `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash` and `PowerShell`. Ask
  rules outrank allow rules from any settings source
  (https://code.claude.com/docs/en/permissions), so the user's or a project's
  own allow rules cannot let an edit or command skip the host. The mode does
  not change these flags, so it can switch without a respawn.
- **Ask mode.** Every such request is a blocking card (`policy: "ask"`).
- **Auto mode.** The host accepts a file-edit request only if its single target
  resolves strictly inside the trusted folder. The resolution follows existing
  symlinks and junctions. `..`, other absolute paths and agent-configuration
  paths are rejected: `.git`, `.claude`, `.codex`, `.mcp.json`,
  `.agent-bridge` and `.vscode`. An edit there could grant the agent new hooks,
  MCP servers or permissions. The allowed input is pinned to the validated
  absolute path. Commands always ask, because Claude Code has no command
  sandbox on Windows. Revoking folder trust takes effect at the next request.
- **Backstop.** If a file-edit or command tool finishes successfully without
  reaching the host, the adapter emits `notice`
  `ran_without_host_approval`, so a bypass is visible, not silent.
- **Unverified live.** Ask-rule precedence and `--settings` merging are taken
  from the docs and fake-CLI tests. They have not been observed in a real turn.

## Context handoff to Codex

Native sessions are not portable between providers. `build_handoff()` produces
a bounded, user-visible packet for the new Codex thread, and the user can edit
it before sending. The packet contains:
- the workspace name
- the Claude session ID, marked "not resumable in Codex"
- the last turn's verdict and the tools used
- files the user selected
- the user's summary
- the tail of Claude's last reply

Diffs are attached by the UI from `git diff` of the selected files. The reverse
direction (Codex to Claude) uses the same packet shape, sent as the first user
turn of a new Claude session.

## Failure behaviour

| Condition | Signal | UI |
|---|---|---|
| CLI missing | `claude_not_installed` | install link |
| Not logged in | preflight `not_logged_in` | "Run `claude auth login` in a terminal", then retry |
| API / gateway / 3P auth | `not_a_subscription_plan` / `not_first_party` / `api_key_source_present` | refuse; explain that no API billing is used |
| CLI too old / newer than tested | `cli_too_old` / `version_status=newer_untested` | block / warn |
| Usage limit | `rate_limit` `status=rejected`; `turn_finished` `ok=false` | banner with the reset time |
| Model refusal fallback | `notice` | inline notice |
| Unknown model | `turn_finished` `ok=false`, `api_error_status=404` | re-open the model menu |
| Process died | `process_exited` | resume button |
| Host timeout (600s with no event) | `turn_finished` `subtype=host_timeout` | interrupt, then offer a resume |

## Slice (implemented in `tools/claude_runtime.py`, driven by `tools/desktop.py`)

```python
pre = preflight()                         # raises RuntimeRefused(reason); pre.models is the live menu
trust.trust(workspace)                    # only after the user clicks "Trust"
consent = CreditConsent(confirmed_by_user=True)   # only if the user said yes to Fable for THIS session
s = ClaudeSession(pre, workspace, model="claude-fable-5[1m]", trust=trust, credit_consent=consent,
                  approval_mode="ask_every_edit", auto_trust=auto_edit_folders)
s.send("..."); for env in s.events(): render(env)   # answer only approval_request with policy "ask"
s.answer_approval(req_id, allow=True)     # or allow=False; emits approval_decision
s.set_approval_mode("auto_accept_trusted", auto_edit_folders)   # between turns
s.set_model("opus"); s.interrupt(); s.close()
ClaudeSession(pre, workspace, model="sonnet", resume=s.session_ref, trust=trust)
```

Acceptance tests, all against `fake_claude_cli.py`, with no model contacted:
- `tools/tests/test_claude_runtime.py`
  - the version guard, including 2.1.201 accepted as `older_untested`, and Windows native-install discovery
  - every row of the event mapping, and the `is_error` verdict
  - explicit trust
  - API-billed login refused, and no model call in preflight
  - planted billing variables never reaching a child
  - a streamed turn, an approval round trip, interrupt and resume
  - a bounded handoff
- `tools/tests/test_claude_desktop_session.py`
  - Fable consent:
    - refused before spawn
    - single-use, with a resumed process asking again
    - mid-session `set_model`
    - a `default` that resolves to Fable in the live menu only
  - the live menu passing through exactly
  - the chat process's own plan re-check
  - an API-key source reported mid-turn causing an interrupt
  - assistant `billing_error` becoming `runtime_error`
  - native ID assigned up front
  - ask rules present and the temp file removed
  - ask mode blocking, with decline leaving the file unchanged
  - auto mode:
    - needs an explicit auto-edit trust
    - pins the in-folder path
    - still asks for outside, traversal, `.claude`, `.mcp.json` and `.git` targets
    - stops at once on revoke
  - no mode change during a turn
  - the `ran_without_host_approval` backstop, with no false flag when `tool_use_id` is absent
  - the `.cmd` argument guard
- `tools/tests/test_desktop_claude.py`: the Tk host's Claude routing, with no window created
  - auto-accepted requests never block on the user
  - a decline is honoured
  - a toggle change reuses the native session
  - consent is consumed by one new session
  - a runtime error does not leak into the next turn
  - a billing refusal surfaces as a reason code

Still to prove on Jerome's PC, which is Windows acceptance and needs his separate authorization:
- a real turn, approval allow and deny, resume, interrupt
- ask-rule precedence
- `taskkill /T` on the `.cmd` shim
- UTF-8 on a real turn

## Open questions

Settled: Fable stays in the menu, and each session needs its own confirmation (prompt 4). Approvals follow the VLI-159 toggle above. The envelope is the shared schema, and the Codex lane maps onto it. The UI is the stdlib Tk host in `tools/desktop.py`.

Still open:
1. **Live confirmation.** The ask-rule routing, auto-accept and `ran_without_host_approval` need one real approval turn on Jerome's PC. That is Windows acceptance, which needs his separate authorization.
2. **Command friction.** Asking for every `Bash`/`PowerShell` command, including read-only ones like `git status`, is safe but noisy. A follow-up could offer "allow this exact command for this session" through the CLI's permission suggestions.
