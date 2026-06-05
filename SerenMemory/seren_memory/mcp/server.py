"""
seren_memory.mcp.server
═══════════════════════

Wires the FastMCP server INTO the existing FastAPI app at /mcp.

DESIGN

Same process, same port. The MCP tools call MemoryStore directly via
captured closures — no HTTP round-trip back to ourselves. One install,
one approval surface, one set of logs. (See seren_memory.mcp.__init__
for the rationale.)

Mounted at /mcp by default; override via the SEREN_MCP_MOUNT env var if
you need to share the namespace with something else.

SDK COMPATIBILITY

The Python `mcp` SDK is moving fast — the modern transport is
"streamable HTTP" (`streamable_http_app()`); older versions used SSE
(`sse_app()`). This module tries the newer call first and falls back,
so it works across a range of installed versions. If neither attribute
exists, we raise a clear error with the SDK version we saw — better than
a cryptic AttributeError at mount time.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def mount_mcp_routes(app: FastAPI) -> None:
    """Mount the SerenMemory MCP server onto an existing FastAPI app.

    Called from seren_memory.app at startup IF the [mcp] extras are
    installed (the import gate in app.py catches ImportError when the
    `mcp` package isn't available).

    Reads app.state.store, app.state.config, and app.state.consolidator
    (set by the lifespan handler) to wire tools to live state.
    """
    # Imported here, not at module top, so an import failure of the
    # `mcp` package bubbles up to app.py's try/except (which falls back
    # to pure-HTTP mode) rather than crashing module load.
    from mcp.server.fastmcp import FastMCP

    from .tools import register_tools

    mount_path = os.environ.get("SEREN_MCP_MOUNT", "/mcp").rstrip("/")
    if not mount_path.startswith("/"):
        mount_path = "/" + mount_path

    store = getattr(app.state, "store", None)
    config = getattr(app.state, "config", None)
    consolidator = getattr(app.state, "consolidator", None)
    if store is None or config is None:
        raise RuntimeError(
            "mount_mcp_routes called before app.state.store/config were set. "
            "Mount inside the lifespan handler after the store is constructed."
        )

    mcp = FastMCP("seren-memory")
    register_tools(mcp, store, config, consolidator)

    asgi_app = _resolve_transport_app(mcp)
    app.mount(mount_path, asgi_app)
    logger.info("[seren-memory] MCP server mounted at %s (%d tools)",
                mount_path, _count_tools(mcp))


def _resolve_transport_app(mcp) -> object:
    """Return an ASGI app for the MCP HTTP transport, tolerating SDK
    version drift. Tries streamable_http (current) then sse (legacy).
    """
    for attr in ("streamable_http_app", "sse_app"):
        factory = getattr(mcp, attr, None)
        if callable(factory):
            logger.info("[seren-memory] MCP transport: %s", attr)
            return factory()
    # Neither known transport factory exists on this SDK version.
    try:
        import mcp as _mcp_pkg
        version = getattr(_mcp_pkg, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        version = "unknown"
    raise RuntimeError(
        f"mcp SDK version {version} exposes neither streamable_http_app "
        "nor sse_app on FastMCP — cannot mount HTTP transport. Try "
        "`pip install -U mcp` or pin a known-good version in extras."
    )


def _count_tools(mcp) -> int:
    """Best-effort tool count for the startup log line. The SDK doesn't
    promise a stable attribute; try a couple of likely shapes, fall back
    to 0 silently (logging is decoration, not core)."""
    for attr in ("_tools", "tools", "_tool_manager"):
        obj = getattr(mcp, attr, None)
        if obj is None:
            continue
        # FastMCP keeps tools in a manager with a .list_tools() or a dict.
        if hasattr(obj, "list_tools"):
            try:
                return len(list(obj.list_tools()))
            except Exception:  # noqa: BLE001
                continue
        if isinstance(obj, dict):
            return len(obj)
    return 0
