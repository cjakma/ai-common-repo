# Apply — OpenClaw

Registers the shared memory as an MCP server for OpenClaw.

> OpenClaw's config schema varies by build. The **URL and transport
> (Streamable-HTTP at `POST /mcp`)** are the stable contract; the surrounding
> key names below may need adjusting to your version. Treat `mcp.json` as a
> template, not a guaranteed drop-in.

## Prerequisites
- Gateway running: `ai-common-repo serve` (`127.0.0.1:28787`).
- OpenClaw installed.

## Steps
1. Copy `mcp.json` into OpenClaw's config directory (commonly
   `~/.openclaw/`), or merge the `mcpServers.ai-common-repo` entry into your
   existing OpenClaw config.
2. If OpenClaw registers MCP servers via a plugins manifest instead, add an
   entry pointing at the same URL with the HTTP transport.
3. Restart OpenClaw and confirm the six tools (`memory_*`, `session_ingest`)
   are listed in its tool/MCP inventory.

## Verify
Trigger `memory_search` from OpenClaw against a term you stored via another
host; the same record should come back (shared DB).

## Rollback
Remove the `ai-common-repo` entry from the OpenClaw config and restart. No data
migration occurs.
