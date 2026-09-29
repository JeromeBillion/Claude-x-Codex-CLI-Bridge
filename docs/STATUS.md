# Delivery status board

The live board for both lanes (see `AGENTS.md`, "Delivery workflow"). Each lane
updates only its own rows. Codex copies it into Linear. This is not the
Windows-ready claim: that needs real turns and resume, sandbox and approval
checks on Jerome's PC.

## Merge order (open PRs)

| # | PR | Owner | Head | Reviewer | Verdict | Blocker / next step |
|---|---|---|---|---|---|---|
| 1 | [#15](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/15) Codex App Server policy schema (VLI-158) | Codex | `40d6ece` | Claude | not reviewed | Claude reviews the generated-schema comparison. |
| 2 | [#14](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/14) Codex per-edit apply gate (VLI-159) | Codex | `03a0208` | Claude | not reviewed | Claude reviews Windows paths, the vote-to-apply order and the approval boundary. |
| 3 | [#18](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/18) collaboration roles (VLI-163) | Codex | `ed0d667` | Claude | not reviewed | Claude reviews role safety, Fable consent and billing visibility. |
| 4 | [#20](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/20) shared conversation and handoff (VLI-160) | Claude | `5ba2df8` | Codex | CHANGES REQUESTED at `3320397`; all 5 fixed at `5ba2df8` | Codex re-reviews at `5ba2df8`. Rebase after #14 and #18 merge. |
| 5 | [#16](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/16) capability inventory (VLI-161) | Codex | `2ec77a8` | Claude | not reviewed | Claude reviews the CLI calls and private-output handling. |
| 6 | [#13](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/13) commit relay and roadmap (VLI-183) | Codex | `ef846eb` | Claude | CHANGES REQUESTED at `bacb8f1` | Claude re-reviews the fix commit `ef846eb`. |
| 7 | [#17](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/17) Windows alpha package (VLI-162) | Codex | `ba6333e` | Claude | not reviewed | Last in the order: rebuild the ZIP from merged main. |

`integration/windows-alpha` (Codex) is a test bed only. Retire it once main has
all of the above.

## Windows acceptance (Jerome's PC): not passed

| Check | State |
|---|---|
| Inventory and no-turn probes for both CLIs | done: no-turn checks only |
| Real turns and native resume (Claude and Codex) | not run, needs Jerome |
| Windows sandbox and approval accept/decline | not run, needs Jerome |
| Limit-to-handoff walkthrough | not run |
