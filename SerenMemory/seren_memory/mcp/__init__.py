"""
seren_memory.mcp
═════════════════

Optional MCP server surface for SerenMemory. Only meaningful when the [mcp]
extras are installed (`pip install seren-memory[mcp]`); without those deps,
this subpackage's modules will fail to import and app.py's mount-attempt
will silently no-op, leaving SerenMemory in pure-HTTP mode.

The MCP tools call into MemoryStore directly (not via HTTP round-trip to
ourselves) since we're mounted INTO the same FastAPI app that owns the
store. Less wire, less latency, fewer failure modes.
"""
