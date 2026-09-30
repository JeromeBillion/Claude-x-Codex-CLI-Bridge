# Delivery status board

The live board for both lanes (see `AGENTS.md`, "Delivery workflow"). Each lane
updates only its own rows. Codex copies it into Linear. This is not the
Windows-ready claim: that needs real turns and resume, sandbox and approval
checks on Jerome's PC.

## Merge order (open PRs)

| # | PR | Owner | Head | Reviewer | Verdict | Blocker / next step |
|---|---|---|---|---|---|---|
| - | none open (Codex) | - | - | - | - | #24 merged as `c718235` after Claude APPROVED at `90fd51a`; 228 OK. |
| - | none open (Claude) | - | - | - | - | #22 merged as `45a9546` after Codex APPROVED at `8e700e3`; 225 OK on main; cleanup done. |

`integration/windows-alpha` was retired after all code PRs merged; its old head
`b5b1ca9` is preserved as `archive/integration-windows-alpha-20260930`.
`pre-publish-backup` remains untouched for Jerome.

PR #15 merged first at `0c7b263` after Claude approved head `03e2386`.
PR #14 merged second at `fd9557b4` after Claude approved head `e08f97b`.
PR #18 merged third at `935b9be` after Claude approved head `fc2aa19`.

## Windows acceptance (Jerome's PC): in progress, not passed

| Check | State |
|---|---|
| Live signed-in CLI probes | Codex 0.158.0-alpha.2.1: Plus, all nine catalog rows completed tiny read-only turns, native restart/resume passed. Claude 2.1.201: first-party Team, Haiku/Sonnet switching, default-model turn, resume, Write allow/deny, interrupt and unknown-model failure. No Fable call. |
| Desktop native resume | GPT-only and Claude-only real turns passed; native IDs and marker recall survived host restart. Claude host fix merged in #22. |
| Windows approvals and boundary | Real Codex staged edit deny/accept and trusted edit passed. An outside-folder command produced a native approval request; decline left its file absent. Claude Write deny/accept and trusted edit passed. Bash and protected `.claude/settings.json` requests blocked for user decision. Trusted Claude writes to a parent path and through a Windows junction also asked, and decline left targets absent. |
| Collaboration and handoff | Real disagreement stayed unapproved with user resolution; a five-turn simple case received both exact-hash approvals after #23's parser. Manual handoff recorded on Send. Simulated limit produced a reviewed packet and successful target Codex turn. |
| Redaction, Stop and failures | Real Tk redaction tests 7 OK; final-text re-scan and fake-token confirmation pass. Claude Stop during an active turn failed it with no later approval. Simulated auth/offline/limit failures mapped to visible reasons and handoff suggestion. |
| Package and final main | 228 tests OK, compileall OK. 48-file ZIP integrity and SHA-256 verified; extracted smoke exit 0; real GPT-only and Claude-only turns plus native restart/resume passed from extracted code. |
| Remaining release check | Claude's independent final acceptance audit and resolution of any finding. One transient 15-second Codex thread-start timeout was observed before a successful retry; document as a reliability risk. |
