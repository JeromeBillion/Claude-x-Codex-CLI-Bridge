# Delivery status board

The live board for both lanes (see `AGENTS.md`, "Delivery workflow"). Each lane
updates only its own rows. Codex copies it into Linear. This is not the
Windows-ready claim: that needs real turns and resume, sandbox and approval
checks on Jerome's PC.

## Merge order (open PRs)

| # | PR | Owner | Head | Reviewer | Verdict | Blocker / next step |
|---|---|---|---|---|---|---|
| - | none open | - | - | - | - | Claude checked final main `7a58d08`: 220 OK, compile OK (no model turns). |

`integration/windows-alpha` was retired after all code PRs merged; its old head
`b5b1ca9` is preserved as `archive/integration-windows-alpha-20260930`.
`pre-publish-backup` remains untouched for Jerome.

PR #15 merged first at `0c7b263` after Claude approved head `03e2386`.
PR #14 merged second at `fd9557b4` after Claude approved head `e08f97b`.
PR #18 merged third at `935b9be` after Claude approved head `fc2aa19`.

## Windows acceptance (Jerome's PC): not passed

| Check | State |
|---|---|
| Inventory and no-turn probes for both CLIs | done: no-turn checks only |
| Real turns and native resume (Claude and Codex) | not run, needs Jerome |
| Windows sandbox and approval accept/decline | not run, needs Jerome |
| Limit-to-handoff walkthrough | not run |
