# Delivery status board

The live board for both lanes (see `AGENTS.md`, "Delivery workflow"). Each lane
updates only its own rows. Codex copies it into Linear. This is not the
Windows-ready claim: that needs real turns and resume, sandbox and approval
checks on Jerome's PC.

## Merge order (open PRs)

| # | PR | Owner | Head | Reviewer | Verdict | Blocker / next step |
|---|---|---|---|---|---|---|
| 1 | [#23](https://github.com/JeromeBillion/Claude-x-Codex-CLI-Bridge/pull/23) collaboration vote parser (VLI-163) | Codex | `a1a0156` code; status commit follows | Claude | pending exact-head review | Provisional inflection finding fixed; 226 Windows tests OK. |
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
| Live signed-in CLI probes | Codex 0.158.0-alpha.2.1: Plus, nine catalog rows, native restart/resume. Claude 2.1.201: first-party Team, Haiku/Sonnet switching, resume, Write allow/deny, interrupt and unknown-model failure. No Fable call. |
| Desktop native resume | GPT-only and Claude-only real turns passed; native IDs and marker recall survived host restart. Claude host fix merged in #22. |
| Windows approvals and boundary | Real Codex staged edit deny/accept and trusted edit passed. Claude Write deny/accept and trusted edit passed. Bash and protected `.claude/settings.json` requests blocked for user decision. Outside-folder Codex attempt left file absent; stronger sandbox attribution remains to assess. |
| Collaboration and handoff | Real disagreement stayed unapproved with user resolution; a five-turn simple case received both exact-hash approvals after #23's parser. Manual handoff recorded on Send. Simulated limit produced a reviewed packet and successful target Codex turn. |
| Remaining release checks | Redaction/re-scan UI exercise, extracted-package live path, model catalog callable sampling, final cross-review/merge and final-main retest. |
