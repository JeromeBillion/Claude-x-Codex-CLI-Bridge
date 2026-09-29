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
review. A repair commit queues a fresh review of that new commit.

## Queue commits

From a worktree, manually queue its latest code commit:

```powershell
python -m tools.agent_relay enqueue-commit
```

After both lanes review the hook, install it once for this repository:

```powershell
git config core.hooksPath .githooks
```

The tracked `.githooks/post-commit` invokes `enqueue-commit` automatically.
If the repository already has a custom `core.hooksPath`, incorporate this hook
there instead of replacing that configuration. The hook only routes owned
branches (`codex/` and `claude/`) and never launches a CLI.

## Review queue

Inspect the queue without a model call:

```powershell
python -m tools.agent_relay status --agent claude
python -m tools.agent_relay status --agent codex
```

To make one tool-disabled review turn, explicitly opt in:

```powershell
python -m tools.agent_relay watch --agent claude --max-turns 1 --allow-model-turns
python -m tools.agent_relay watch --agent codex --max-turns 1 --allow-model-turns
```

Run each watcher from its own worktree. It waits for at most one queued
request, then exits. `--max-turns` accepts 1–20 but **1 is the recommended
cash-controlled default**. Without `--allow-model-turns`, `watch` only reports
pending count. No Fable or `best` model is chosen for unattended review.
Claude's workspace must already be explicitly trusted for a headless session
in the desktop app; the relay does not grant trust. A review request whose
provider fails is marked failed for inspection, not silently retried.
Claude's own SessionStart hooks may still run when its process starts, even
with CLI tools disabled, which is why workspace trust is required.

Read a saved review using the full SHA:

```powershell
python -m tools.agent_relay show-report <full-sha> --agent claude
python -m tools.agent_relay retry <full-sha> --agent claude
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
