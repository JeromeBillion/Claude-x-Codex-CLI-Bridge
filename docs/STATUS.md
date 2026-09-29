# Delivery status board

The live board for both lanes (see `AGENTS.md`, "Delivery workflow"). Each lane
updates only its own rows. Codex copies it into Linear. This is not the
Windows-ready claim: that needs real turns and resume, sandbox and approval
checks on Jerome's PC.

## Merge order (open PRs)

| # | PR | Owner | Head | Reviewer | Verdict | Blocker / next step |
|---|---|---|---|---|---|---|
| 4 | [#20](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/20) shared conversation and handoff (VLI-160) | Claude | `6e185d7` | Codex | APPROVED at `6e185d7` | **Merged** as `111ba18`; 198 OK on main. Branch cleanup is waiting on Jerome's permission rule. |
| 5 | [#16](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/16) capability inventory (VLI-161) | Codex | `2ec77a8` | Claude | not reviewed | Claude reviews the CLI calls and private-output handling. |
| 6 | [#13](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/13) commit relay and roadmap (VLI-183) | Codex | `ef846eb` | Claude | APPROVED at `ef846eb` | Merge when it is next in order. Three non-blocking follow-ups are on the PR. |
| 7 | [#17](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/17) Windows alpha package (VLI-162) | Codex | `ba6333e` | Claude | not reviewed | Last in the order: rebuild the ZIP from merged main. |

`integration/windows-alpha` (Codex) is a test bed only. Retire it once main has
all of the above.

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
