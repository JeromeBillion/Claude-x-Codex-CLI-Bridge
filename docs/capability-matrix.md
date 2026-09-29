# Local CLI capability matrix (VLI-161)

The desktop's **Inspect capabilities** button queries the installed providers
after Connect. It does not send a model turn or invoke a tool. Results are
shown only in the local window, not saved in the repository or transcript.
Claude's MCP listing may health-check configured servers. The subprocess gets
the same restricted environment as the Claude probe, which disables automatic
Claude.ai MCP connector loading. This view therefore covers local MCP servers,
not every connector visible in Claude.ai.

| Provider family | Source | What the desktop can show | What remains unproved |
| --- | --- | --- | --- |
| Codex skills | `skills/list` for chosen workspace | Name and enabled state | A skill runs successfully in a turn |
| Codex MCP | Paginated `mcpServerStatus/list` | Name, reported sign-in need and advertised tool count | Tool call, approval behavior, server health in a turn |
| Codex installed apps | `app/installed` | Name and CLI's enabled/callable flags | Account entitlement, consent and successful connector call |
| Claude plugins | `claude plugin list --json` | Name and enabled state | Plugin command/tool works in a headless session |
| Claude local MCP | `claude mcp list` | Name and health-check result | Tool call or connector parity with Claude.ai |

The installed Windows CLI inventory on September 29, 2026 showed 110 enabled
Codex skills for this repository, 8 Codex MCP servers (3 reporting a sign-in
need), and 13 installed apps advertising callable access. The Claude CLI
reported 21 enabled plugins and 4 connected local MCP servers under the
restricted inventory environment. These are **discovery counts**. They can
change with CLI versions, account state, workspace and user configuration.

The desktop does not yet implement dedicated consent forms for Codex MCP
elicitation, permission subsets and connector user input. Those requests stay
blocked. Neither consumer ChatGPT nor Claude.ai connector parity is claimed.
The next acceptance step is a controlled real tool call per provider in a
disposable project, with visible allow/decline and failure recovery, before
marking a family callable in this matrix.
