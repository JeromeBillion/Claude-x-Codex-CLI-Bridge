# Claude Code desktop runtime adapter (VLI-158/159)

Status: design plus a first vertical slice (`tools/claude_runtime.py`). The
slice is tested against a fake CLI and CLI 2.1.283 in a Linux container. It is
**not yet proven on Jerome's Windows PC or his plan.**

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
   - **Version.** Parse `claude --version`. Below `MIN_VERSION` (2.1.280) the adapter refuses with `cli_too_old`. Above `TESTED_VERSION` it runs but shows "untested CLI". Re-run the probe after every CLI update.
   - **Auth.** `claude auth status --json` must exit 0 and report `loggedIn`.
   - **Plan.** Start a stream-json process with no tools and send `control_request` `initialize`. `account.subscriptionType` must be Claude Pro, Max, Team or Enterprise. `apiProvider` must be `firstParty` and no `apiKeySource` may be present. Otherwise the adapter refuses (`not_a_subscription_plan`, `not_first_party` or `api_key_source_present`). "Claude API" is refused.
   - The same response gives `models[]`, the model menu.
3. **Trust gate.** `-p` skips the CLI's workspace trust dialog and runs that repo's hooks and `.mcp.json` servers without asking (headless docs). The app therefore keeps its own `TrustStore`. The first session in a folder requires an explicit "Trust this folder" click, and the dialog lists `.claude/settings*.json` hooks and `.mcp.json` servers.
4. **Spawn.** Run one long-lived process per chat, with `cwd` set to the workspace and the child environment passed through `child_env()`:
   ```
   claude --print --input-format stream-json --output-format stream-json --verbose
          --include-partial-messages --permission-prompt-tool stdio --permission-mode default
          --model <menu value> (--session-id <uuid> | --resume <id>)
   ```
   - `child_env()` removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the Bedrock, Vertex and Foundry variables.
   - Never pass `--bare`, which ignores OAuth. The docs say bare mode will become the default for `-p`, and the version guard exists to catch that.
   - User and project settings, plugins and MCP servers load as normal. That is the point of a personal workspace, and the trust gate covers it.
5. **Close.** Close stdin, wait, then kill if needed. On Windows, kill with `taskkill /T` for `.cmd` shims, as the bridge already does. SIGTERM or a kill mid-turn leaves the turn unfinished, so the UI sends `interrupt` first.

## Provider-neutral envelope

`Envelope(provider, session_ref, kind, data)`. Codex's lane maps its app-server
events onto the same kinds. `session_ref` stays each provider's native ID and
is never translated.

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
| process EOF | `process_exited` | `exit_code` | "runtime stopped", with a resume button |

Rules:
- **Turn verdict.** It comes from `is_error`, never from the exit code or `subtype`. An unknown model was observed returning `subtype: "success"`, `is_error: true`, 404 and exit code 1.
- **Unknown event types** are dropped, not treated as errors, so a CLI upgrade can add events without breaking the UI.
- **Approval answers.** Allow sends exactly the input the user saw, or the version the user edited. Deny sends a message. An approval ID the CLI never issued is refused.

## Models, switching, interrupt, resume

- **Menu.** Always build it from `initialize.models` (`value`, `resolvedModel`, `displayName`, effort levels). Never hard-code names. Fable, Opus, Sonnet and Haiku appeared in the container's menu; Jerome's plan menu is **unproven**.
- **`set_model`.** A `control_request` `set_model` switches the model mid-session; this was observed switching Haiku to Sonnet. It is sent between turns.
- **Hidden credit costs.** In `-p`, Fable "bills it without asking" when a request would draw on usage credits. The adapter refuses Fable models, both at spawn and on `set_model`, unless the UI passes `allow_credit_models=True` after an explicit per-session confirmation.
- **Interrupt.** A `control_request` `interrupt` ends the turn with `turn_finished` `ok=false`, `terminal_reason: aborted_streaming`.
- **Resume.** New chats get `--session-id <uuid>` and reopened chats get `--resume <uuid>`; `--fork-session` branches a chat.
  - The CLI owns the transcript (`%USERPROFILE%\.claude\projects\…\<id>.jsonl`). Its format is internal and it is pruned after 30 days.
  - The app keeps its own envelope log for rendering history, and treats the session ID only as a resume handle.
- **Consumer chats.** There is no import from claude.ai consumer chats. No supported interface exists for it, and the app does not claim one.

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

## First vertical slice (implemented in `tools/claude_runtime.py`)

The slice covers one chat: preflight, trust gate, a streamed turn, one approval
round trip, interrupt, `set_model`, `--resume`, and a handoff packet. It has no
GUI yet. The desktop shell (VLI-158) consumes `ClaudeSession.events()`.

```python
pre = preflight()                         # raises RuntimeRefused(reason)
trust.trust(workspace)                    # only after the user clicks "Trust"
s = ClaudeSession(pre, workspace, model="sonnet", session_id=new_uuid, trust=trust)
s.send("…"); for env in s.events(): render(env)
s.answer_approval(req_id, allow=True)     # or allow=False
s.set_model("opus"); s.interrupt(); s.close()
ClaudeSession(pre, workspace, model="sonnet", resume=session_id, trust=trust)
```

Acceptance tests (`tools/tests/test_claude_runtime.py`, against `fake_claude_cli.py`):
- version guard and Windows native-install discovery
- every row of the event mapping, and the `is_error` verdict
- trust is explicit and persisted
- an API-billed login is refused, and preflight makes no model call
- a planted `ANTHROPIC_API_KEY` never reaches a child
- an untrusted workspace and silent Fable (at spawn and on `set_model`) are refused
- streamed turn → `text_delta`, `rate_limit`, `notice`, `turn_finished`
- an approval allows exactly the proposed input, and a forged approval ID is refused
- interrupt produces `aborted_streaming`
- `--resume` is passed through
- the handoff packet is bounded and disclaims native transfer

Still to prove on Jerome's PC, using `python tools\claude_runtime_probe.py`
first without flags and then with `--turns`:
- his plan label and model menu
- `.exe` and `.cmd` spawning
- `taskkill /T` behaviour
- UTF-8 on the console

## Open questions

1. **Credit confirmation.** Does Jerome want Fable at all in the desktop app? If yes, should confirmation be per session or per turn?
2. **Default approvals.** Should the default policy be "ask for everything" (`default` mode) or `acceptEdits` inside a trusted workspace? The slice uses `default`.
3. **Codex lane alignment.** Should the envelope kinds above become the shared schema? The Codex reply should confirm, or propose a mapping for its item and approval events.
4. **UI technology (VLI-158).** This adapter is stdlib Python. A Tauri or Electron shell would either spawn it as a sidecar or port the same protocol to TypeScript. The protocol is the contract; the implementation can be ported.
