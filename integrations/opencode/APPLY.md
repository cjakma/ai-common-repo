# Apply — OpenCode

OpenCode speaks MCP natively. The shared memory is registered as a *remote*
(HTTP) MCP server.

## Prerequisites
- Gateway running: `ai-common-repo serve` (`127.0.0.1:28787`).
- OpenCode installed.

## Steps
1. Merge `opencode.json` into your config:
   - Global: `~/.config/opencode/opencode.json`
   - Per-project: `opencode.json` at the repo root (takes precedence).
   If you already have an `mcp` block, add only the `ai-common-repo` key.
2. Restart OpenCode. The six tools become available to the agent
   (prefixed by the server name, e.g. `ai-common-repo_memory_search`).

## Verify
Ask the agent to call `memory_search` for a known term, or check OpenCode's MCP
status panel for `ai-common-repo` = connected.

## Rollback
Remove the `ai-common-repo` entry from the `mcp` block (or set
`"enabled": false`). No data migration occurs.
