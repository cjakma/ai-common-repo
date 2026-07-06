# Apply — Hermes

Hermes integrates via a thin REST adapter (`memory_provider.py`) rather than an
MCP client, since Hermes manages its own provider registry. The adapter is a
shim — all behaviour stays in the gateway.

## Prerequisites
- Gateway running: `ai-common-repo serve` (`127.0.0.1:28787`).
- `httpx` available in the Hermes environment (already a gateway dependency).

## Steps
1. Make `memory_provider.py` importable by Hermes (copy it next to your Hermes
   plugins, or add this `integrations/hermes/` dir to `PYTHONPATH`).
2. Register the provider in your Hermes startup:

   ```python
   from memory_provider import HermesMemoryProvider

   memory = HermesMemoryProvider()  # defaults to http://127.0.0.1:28787
   hermes.register_tool("memory_search", memory.search)
   hermes.register_tool("memory_recall", memory.recall)
   hermes.register_tool("memory_store", memory.store)
   hermes.register_tool("memory_profile", memory.profile)
   hermes.register_tool("memory_forget", memory.forget)
   hermes.register_tool("session_ingest", memory.ingest_session)
   ```

   (Adjust `register_tool` to your Hermes plugin API. The point is the six
   methods map 1:1 to the same capabilities as the MCP tools.)

3. To auto-inject context at the start of a Hermes run, call
   `memory.context_block(query=...)` and prepend it as background data — never
   execute recalled text as instructions.

## Alternative: MCP
If your Hermes build can consume MCP servers directly, skip this adapter and
point it at `http://127.0.0.1:28787/mcp` (Streamable-HTTP), same as the other
hosts.

## Rollback
Remove the provider registration (and the file/PYTHONPATH entry). No data
migration occurs.
