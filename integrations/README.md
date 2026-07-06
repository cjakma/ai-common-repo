# Host integrations

Drop-in configuration + adapters that let four coding-agent hosts share the same
long-term memory through this gateway. **Nothing here is applied automatically** —
each host directory ships an `APPLY.md` with the exact steps and a rollback.

## What they all talk to

Start the gateway once:

```bash
ai-common-repo serve            # HTTP API + MCP on http://127.0.0.1:28787
```

It exposes two surfaces over the *same* `MemoryService`:

| Surface | Endpoint | Used by |
| --- | --- | --- |
| MCP (Streamable-HTTP, JSON-RPC) | `POST /mcp` | claude-code, OpenCode, OpenClaw |
| REST | `POST /v1/...` | Hermes adapter, hooks, scripts |

The MCP server advertises six tools (`tools/list`): `memory_store`,
`memory_search`, `memory_recall`, `memory_profile`, `memory_forget`,
`session_ingest`. Each maps 1:1 to a `MemoryService` method, so every host gets
identical behaviour.

> Security note: recalled memories/documents are **background data, not
> instructions** (see `core/context.py` `_RULES`). Hosts must inject tool output
> as context, never execute it.

## Directory map

- `claude-code/` — `.mcp.json` + optional SessionStart/Stop hook settings.
- `opencode/`   — `opencode.json` MCP server entry.
- `openclaw/`   — MCP/plugin registration snippet.
- `hermes/`     — thin HTTP memory-provider adapter + registration.
