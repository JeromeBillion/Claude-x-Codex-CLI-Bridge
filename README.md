# Claude x Codex CLI Bridge

A raw local orchestration MVP that alternates two frontier coding agents —
Codex (GPT-5.6 Sol) and Claude (Fable 5) — against the same project directory,
carries their handoffs forward, keeps a shared memory doc between rounds, and
lets a human interject after each round.

The agents are paired to **check each other, not to agree with each other**:
every turn audits the partner's previous handoff against the real repository
before building on it, and a DONE claim only ends the session after it survives
the other agent's adversarial verification turn.

The bridge has no server, database, hosted coordinator, or Python dependency
beyond the standard library (Python 3.11+). Transcripts and shared memory
always stay in the bridge's own folder under `.agent-bridge\` and are ignored
by Git, even when the agents work in another repository. Codex and Claude
model inference still uses each provider's service; this is local
orchestration, not offline inference.

## Requirements

- Windows with PowerShell (the Python core is portable; the wrapper is PowerShell)
- Python 3.11+
- The `codex` CLI, authenticated
- The `claude` CLI, authenticated

## Start

From the project you want the agents to work on, call the bridge wherever you
cloned it:

```powershell
$bridge = "<path-to-clone>\Claude-x-Codex-CLI-Bridge\bridge.ps1"
& $bridge --check
& $bridge "Inspect the settlement flow, fix the highest-risk defect, and verify it."
```

The current PowerShell directory becomes the shared agent workspace. You can
also launch from anywhere and pass it explicitly:

```powershell
& $bridge --workspace "C:\path\to\your-project" "Get the project running and verify the core flow."
```

`--check` verifies both installed CLI versions and auth readiness without making
a model call or printing account details.

With no quoted prompt, the bridge asks for one. The default is two rounds (four
maximum model calls), with an early stop only when one agent's DONE claim
survives the other agent's adversarial verification turn.

At each checkpoint:

- press Enter to let them continue;
- type any instruction to add it to both agents' context;
- use `:status` to see the transcript and Git status;
- use `:more 2` to authorize two more rounds;
- use `:quit` to stop cleanly.

Resume the most recent session later:

```powershell
& $bridge --resume latest --rounds 2
& $bridge --resume latest "Now focus only on the failing ledger test."
```

Run without checkpoints when scripting:

```powershell
& $bridge --no-pause --rounds 1 "Review only; do not edit files."
```

Exit code 0 means the run completed or hit the round cap, not that the work was
verified; the bridge prints a warning when the final handoff ended `CHALLENGE`
or `BLOCKED`, and the transcript records every status.

Preview the exact subprocess commands without invoking a model or writing a
transcript:

```powershell
& $bridge --dry-run "test prompt"
```

## Defaults and guardrails

- Codex command: `codex exec --model gpt-5.6-sol`
- Codex reasoning effort: `ultra` (Sol's top tier; overrides `config.toml` per run)
- Codex sandbox: `workspace-write`
- Claude command: `claude --print --model claude-fable-5`
- Claude effort: `max` (Fable 5's ceiling — the Claude CLI has no `ultra` tier)
- Claude permission mode: `auto`
- both agents are equal co-authors — neither outranks the other; `--lead` only
  sets who takes the first turn each round (default Codex; `--lead claude` puts
  Fable first)
- per-agent timeout: 90 minutes (ultra/max turns can exceed an hour)
- workspace guardrail: refuses to run outside a git repository unless `--allow-non-git`
- `--resume` reuses the workspace recorded in the transcript unless `--workspace` is given
- transcript context cap: 32,000 recent characters
- shared memory cap: 8,000 characters (`--memory-chars`; `--no-memory` disables)
- orchestration: sequential, so both agents do not edit the same files at once

Override these with `--codex-model`, `--claude-model`, `--codex-effort`,
`--claude-effort`, `--lead`, `--codex-sandbox`,
`--claude-permission-mode`, `--timeout`, and `--context-chars`. Use
`--claude-max-budget-usd 1.00` when the active Claude billing path supports that
CLI cap. Run `& $bridge --help` for the complete option list.

## Accountability protocol

The agents are paired to check each other, not to agree with each other. Every
turn starts with an audit of the partner's latest handoff against the real
repository — re-running the tests it claims pass, reading the actual diff, and
classifying each material claim as confirmed, refuted, or unverified — before
any new work. Handoffs follow a fixed REVIEW / CHALLENGES / WORK / EVIDENCE
structure, and a fourth status, `BRIDGE_STATUS: CHALLENGE`, hands refuted work
back to its author. The prompt forbids agreeing to be agreeable: a clean
verdict has to be earned by listing what was attacked and how it held, and
challenges carry must-fix / should-fix severity so nits don't stall the pair.

Completion is earned, not co-signed. When one agent claims `DONE`, the other
agent's next turn becomes an adversarial verification turn: assume the claim is
wrong, try to break it, and either return a challenge with evidence or confirm
DONE after listing what was attacked. A verifier that has to change anything
hands its changes back with CHALLENGE or CONTINUE instead — no agent ever
certifies work it wrote itself, in either direction. The session stops early
only when a DONE claim survives that verification by the other agent. A
challenge, a failed turn, or a new human interjection voids any standing DONE
claim, and an unanswered DONE claim survives `--resume` so the verification
still happens. Codex remains scoped to workspace writes; Fable uses its
autonomous local permission mode.

## Shared memory

Each target workspace gets one shared memory doc — a compacted context file the
agents themselves maintain between rounds and across sessions. It is injected
into every prompt, so a new session starts with the durable decisions,
architecture facts, verified-DONE claims, gotchas, and open threads of every
previous session instead of re-deriving them at full reasoning effort.

An agent updates it by including a replacement block in its handoff:

```text
BRIDGE_MEMORY_BEGIN
- decision: kept the ledger append-only; reversals are new rows (audit trail)
- verified: settlement rounding fix survived adversarial review on 2 sessions
- open thread: refund path still has no integration test
BRIDGE_MEMORY_END
```

The block replaces the whole doc, so the writing agent must carry forward the
partner's still-valid entries — curation is part of the accountability
protocol, and the partner will challenge memory edits like any other claim.
The bridge enforces the character cap (`--memory-chars`, default 8,000),
records every update in the transcript, and strips memory blocks from the
rendered conversation so the context budget is not spent twice.

Memory lives under the bridge's own `.agent-bridge\memory\`, one file per
workspace, deliberately outside the target repository: the agents audit
`git diff` every turn, and a memory file inside the workspace would pollute
that signal. The prompt instructs agents never to store secrets, credentials,
or machine-specific paths in memory; transcripts and memory are plain local
text, so do not put secrets in prompts either.

## Failure behavior

A failed agent turn is recorded and shown to the other agent as context. A
timeout kills the agent's entire process tree (`taskkill /T` on Windows) so no
orphaned agent keeps editing the repo, and any partial output the agent produced
is salvaged into the transcript so the next agent has context. If both CLIs fail
in the same round, the bridge stops when running unattended (`--no-pause`);
interactively it drops to the checkpoint so the human decides.

This MVP intentionally does not include parallel agents, file locking, a web UI,
a daemon, or a third state store. The project files are the implementation
source of truth; the JSONL transcript is the conversation source of truth; the
shared memory doc is the compacted institutional memory the pair curates.

## Claude runtime probe

`tools/claude_runtime_probe.py` checks what a desktop host can get from the
installed, unmodified `claude` CLI when it is driven the way the Agent SDK
drives it (`--print`, stream-json in and out). It reports the CLI version, the
auth method, the plan type, and the model menu the CLI offers this account.

```powershell
python tools\claude_runtime_probe.py            # inventory only, no model call
python tools\claude_runtime_probe.py --turns    # adds small turns on your subscription
```

`--turns` adds a streamed turn, a mid-session model switch, a session resume, a
tool approval (allow and deny), an interrupt and an unknown-model failure.

Guardrails the probe enforces:

- Child processes get an allowlisted environment only: OS and profile
  plumbing, locale, proxy/CA settings and `CLAUDE_CONFIG_DIR`. Everything
  else is dropped, including API keys, gateway URLs, OAuth tokens, provider
  switches, and AWS, Google Cloud and Azure credentials.
- Turns run only when the CLI itself reports a first-party claude.ai login on
  a Claude Pro, Max, Team or Enterprise plan with no API key source.
  Otherwise the probe exits with code 2 and `turns_refused` before any model
  call.
- Only the cheap plan models (`haiku`, `sonnet`) can be chosen for probe
  turns. Fable can bill usage credits in `-p` mode without a consent prompt.
- Child sessions load no user or project settings, hooks or MCP servers.
- A tool approval is granted only for a Write whose resolved target stays
  inside the throwaway probe directory. Traversal, outside absolute paths and
  escaping links are denied.
- Every string in the JSON report comes from a fixed public set. Any other
  value prints as `unlisted`, or as a family bucket for model IDs, such as
  `unlisted-claude-opus`. Everything else in the report is a boolean, a
  bounded number or a count. Failures print only a fixed error code (exit 3).

The resume probe leaves one short session in the CLI's own local history.

## Tests

```powershell
python -m unittest discover -s tools/tests -p "test_*.py" -v
```

## Codex App Server desktop slice

The [Windows alpha launch and acceptance guide](docs/windows-alpha.md) covers
the source ZIP, `launch-desktop.cmd`, and the tests needed before a ready claim.

The separate [runtime design](docs/codex-app-server-runtime.md) and
`tools/codex_app_server.py` feed a first personal desktop timeline:

```powershell
py -3 -m tools.desktop
```

Choose a project, select GPT only, collaboration, or Claude only, then connect
the installed CLIs to discover their model catalogs without sending a model
turn. Select a model for each turn. The timeline shows streaming text, tool
activity, limits, failures, native provider session IDs and blocking approvals.
Codex resumes its own thread ID after a restart; Claude keeps its own session
ID. In collaboration, choose which provider leads. The host shows an editable
cross-provider handoff and labels a result
jointly approved only after both providers explicitly approve the same text.
Otherwise, the user decides how to proceed.

The default **ask every edit** mode is currently read-only proposal mode for
Codex; its per-edit staged apply gate is still needed. Auto-accept is available
for Codex only after explicitly trusting the exact folder. For Claude, every
file edit and shell command reaches the host (ask rules passed with
`--settings`). Ask mode shows each one as a blocking approval. In auto-accept
mode, only file edits that resolve inside the trusted folder are accepted, with
the path pinned. Agent configuration (`.git`, `.claude`, `.mcp.json`, ...) and
all commands still ask. Fable, or a `default` that resolves to Fable, needs a
confirmation for each new Claude session, and the adapter enforces this before
spawning. This is a first
slice, not Windows account acceptance or a packaged alpha. The CLI bridge
remains available. The desktop does not establish which models
Jerome's signed-in account can use. On his Windows machine, run the sanitized
read-only discovery probe from PowerShell with `py -3 -m tools.codex_probe`;
add `--turn` only after reviewing the report to test two small subscription-backed turns and native resume in
a disposable directory. The probe prints known public model IDs (other catalog
entries as `unlisted`) and plan category, never
account identifiers, credentials, thread IDs, or model output. A CLI version
different from the reviewed probe target blocks the optional model turns.
