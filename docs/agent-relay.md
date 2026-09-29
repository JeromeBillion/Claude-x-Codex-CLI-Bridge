# Local Claude–Codex commit relay

The relay shares only commit metadata and local review results between Git
worktrees. Its queue lives under the repository's **common `.git` directory**,
outside version control. Commit hooks enqueue metadata; they never start a
provider or block a commit. Only an explicitly started watcher can spend a
subscription-backed model turn. The watcher has a fixed maximum turn count,
chooses a non-credit Claude model, declines tool approvals, and saves its
review locally. No API keys, private transcripts or review output are pushed.

## Separate worktrees

Codex works on `codex/*`; Claude works on `claude/*`. Never switch a shared
checkout's branch while the other lane is editing. The branch prefix controls
which lane receives a commit. `main` and COMMS-only commits do not queue a
review. A repair commit queues a fresh review of that new commit. If several
commits on one branch are still pending, older requests become `superseded`
audit records and only the latest waits for a paid review.

## Queue commits

From a worktree, manually queue its latest code commit:

```powershell
python -m tools.agent_relay enqueue-commit
```

After both lanes review the hook, install it once for this repository:

```powershell
python -m tools.agent_relay install-hook
```

Installation copies the tracked hook and standalone queue script into the
repository's common `.git/hooks` directory, then sets an absolute
`core.hooksPath`. That one hook runs even when a linked worktree's branch
predates the relay files. An existing custom hook is preserved and installation
refuses to replace it; incorporate the relay hook there manually. Failures are
recorded in local `agent-relay/hook-errors.log`, and `status` reports whether
the hook is effective in the current worktree. The hook only routes owned
branches (`codex/` and `claude/`) and never launches a CLI.

## Review queue

Inspect the queue without a model call:

```powershell
python -m tools.agent_relay status --agent claude
python -m tools.agent_relay status --agent codex
```

To make one isolated Claude review turn, explicitly opt in:

```powershell
python -m tools.agent_relay watch --agent claude --max-turns 1 --allow-model-turns
```

Run each watcher from its own worktree. It waits for at most one queued
request, then exits. `--max-turns` accepts 1–20 but **1 is the recommended
cash-controlled default**. Without `--allow-model-turns`, `watch` only reports
pending count. No Fable or `best` model is chosen for unattended review.
Claude's workspace must already be explicitly trusted for a headless session
in the desktop app; the relay does not grant trust. A review request whose
provider fails is marked failed for inspection, not silently retried.
Claude reviews disable built-in tools, configured MCP servers, claude.ai MCP
connectors, and user/project setting sources. Workspace trust is still required
before opening the session. Codex unattended relay reviews currently fail
closed: a read-only App Server sandbox does not establish a pre-call veto for
MCP and app tools. Codex can review Claude's PR manually under user control
while that isolation remains unverified.

Read a saved review using the full SHA:

```powershell
python -m tools.agent_relay show-report <full-sha> --agent claude
python -m tools.agent_relay retry <full-sha> --agent claude
python -m tools.agent_relay retry <full-sha> --agent claude --from-done
```

If a watcher process dies while holding a claim, stop all watchers for that
agent and then use `retry <full-sha> --agent claude --recover-processing`.

The author addresses findings on a new commit and sends it for review again.
Codex records ticket evidence in Linear. Claude leaves its commit and review
evidence in `comms/claude/COMMS.md` when it cannot access Linear; Codex moves
that evidence to the issues. Both lanes verify claims against the actual diff
and tests before they merge.

## Limits of this first relay

It automates **review triggering**, not continuous autonomous implementation.
The initiating agent still decides and commits the repair. A watcher must be
started explicitly, stays in the foreground, and uses one capped review turn
by default. This keeps cost, approval and feedback-loop behavior inspectable.
The CLI adapters have unit/fake-provider coverage; no subscription-backed
relay review or unattended Windows run has been accepted yet.
