# Apply — claude-code

Wires Claude Code to the shared memory through MCP (six tools), plus optional
session hooks for automatic context-in / ingest-out.

## Prerequisites
- Gateway running: `ai-common-repo serve` (listens on `127.0.0.1:28787`).
- Claude Code CLI installed and authenticated.

## 1. MCP server (recommended)
Copy `.mcp.json` to your project root (Claude Code auto-discovers `.mcp.json`),
**or** register it user-wide:

```bash
claude mcp add --transport http ai-common-repo http://127.0.0.1:28787/mcp
```

Verify inside Claude Code: `/mcp` should list `ai-common-repo` with six tools
(`memory_store`, `memory_search`, `memory_recall`, `memory_profile`,
`memory_forget`, `session_ingest`).

## 2. Session hooks (optional)
Merge `settings.hooks.json` into `~/.claude/settings.json` (global) or
`.claude/settings.json` (per-project). Requires the `claudecode-memory` adapter
on PATH (`pip install -e .` from this repo installs it).

## Rollback
- MCP: delete `.mcp.json`, or `claude mcp remove ai-common-repo`.
- Hooks: remove the `SessionStart`/`Stop` blocks you merged.
- No data is migrated; removing the config only detaches the host.
