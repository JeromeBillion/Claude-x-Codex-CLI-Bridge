# Claude x Codex Desktop roadmap

Codex owns the Linear board, priorities, dependencies and readiness report.
Claude and Codex use separate worktrees, exchange commit reviews through the
local relay, and challenge each other's completion claims. Only focused,
passing work is merged to this repository's main. The product remains a
personal-use Windows coding workspace until acceptance proves otherwise.

## Windows release order

| Order | Linear | Exit evidence |
| --- | --- | --- |
| 1 | [VLI-157](https://linear.app/vlive-projects/issue/VLI-157) | Signed-in CLI inventories, two real turns and native resume for each provider; no API-key billing; Windows sandbox and approval accept/decline checked. |
| 2 | [VLI-159](https://linear.app/vlive-projects/issue/VLI-159) | Model switching, streamed timeline and both approval modes verified. Codex default needs a real per-edit staged apply gate. |
| 3 | [VLI-160](https://linear.app/vlive-projects/issue/VLI-160) | Persisted inspectable local conversation, curated handoff with edit/omit controls, safe redaction and limit-to-handoff walkthrough. |
| 4 | [VLI-183](https://linear.app/vlive-projects/issue/VLI-183) | Bounded local commit relay, independent cross-review evidence and no runaway model turns. |
| 5 | [VLI-158](https://linear.app/vlive-projects/issue/VLI-158) / [VLI-163](https://linear.app/vlive-projects/issue/VLI-163) | CLI regression and shared core evidence; both providers agree before joint output, with disagreement and failure decisions tested. |
| 6 | [VLI-161](https://linear.app/vlive-projects/issue/VLI-161) / [VLI-162](https://linear.app/vlive-projects/issue/VLI-162) | Plugin/MCP capability inventory, packaged Windows alpha and first-user acceptance. |

These can overlap where changes are independent. The order describes release
dependencies, not a requirement to leave another lane idle. Source-specific
findings and test results belong on the corresponding Linear issues.

## Mac after Windows

[VLI-184](https://linear.app/vlive-projects/issue/VLI-184) adds macOS support
after the Windows alpha passes. Reuse the provider-neutral core and build Mac
process, path, sandbox, trust and packaging adapters. Release only after tests
on a real Mac with the installed signed-in CLIs, turn/resume and approval
checks. Do not assume Windows acceptance proves Mac behavior.

## Readiness rule

“Ready for Jerome to use on Windows” means the installed alpha can be opened,
can use his own CLI sign-ins and every callable model the CLIs expose, can
switch providers with a visible handoff, handles limits and disagreement, and
keeps edits and approvals within the selected mode. The actual Windows run
must prove these. Unit tests, a catalog menu or a successful window launch
alone do not satisfy the rule.
