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


def mount_mcp_routes(app: FastAPI):
    """Mount the SerenMemory MCP server onto an existing FastAPI app.

    Called from seren_memory.app at startup IF the [mcp] extras are
    installed (the import gate in app.py catches ImportError when the
    `mcp` package isn't available).

    Reads app.state.store, app.state.config, and app.state.consolidator
    (set by the lifespan handler) to wire tools to live state.

    Returns the FastMCP instance. The caller MUST enter
    `mcp.session_manager.run()` for the lifetime of the app (the streamable
    -HTTP transport's task group lives there); see app.py's lifespan. Older
    SDKs without a session_manager attribute just won't have one to run.
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

    # ── Bug 1: the double-/mcp footgun ──
    # streamable_http_app()/sse_app() serve their endpoint at the path in
    # settings.streamable_http_path, which DEFAULTS TO "/mcp". If we then
    # app.mount(mount_path="/mcp", asgi_app), the real endpoint lands at
    # mount_path + "/mcp" = "/mcp/mcp", and "/mcp" itself 404s. Push the
    # sub-app's own path to root so mount("/mcp", ...) resolves to exactly
    # "/mcp". hasattr-guarded so older SDKs (sse-only) don't choke on a
    # setting they don't have.
    if hasattr(mcp.settings, "streamable_http_path"):
        mcp.settings.streamable_http_path = "/"

    # ── Bug 3: DNS-rebinding host check vs. cross-host LAN access ──
    # FastMCP >=1.x ships DNS-rebinding protection that validates the Host
    # header against allowed_hosts, defaulting to localhost-only
    # (127.0.0.1:*, localhost:*, [::1]:*). That silently 421s the exact
    # use case this route exists for: VSCode/Copilot on a workstation
    # reaching the NUC as `nuc:7420`. The rest of Seren is trusted-LAN with
    # the optional bearer token (see app.py bearer_auth) as the real gate,
    # so we default the host check OFF to match that posture. Operators who
    # want it back on set SEREN_MCP_ALLOWED_HOSTS (comma-sep, e.g.
    # "nuc:*,192.168.0.200:*,localhost:*") and optionally
    # SEREN_MCP_ALLOWED_ORIGINS. hasattr-guarded for SDK drift.
    if hasattr(mcp.settings, "transport_security"):
        _apply_transport_security(mcp)

    asgi_app = _resolve_transport_app(mcp)
    app.mount(mount_path, asgi_app)
    logger.info("[seren-memory] MCP server mounted at %s (%d tools)",
                mount_path, _count_tools(mcp))

    # ── Bug 2: the mounted sub-app's lifespan never runs ──
    # Returned so the caller (app.py lifespan) can run the streamable-HTTP
    # session manager's task group. Starlette does NOT fire a mounted
    # sub-app's lifespan, and the session manager raises "Task group is not
    # initialized" on first request unless session_manager.run() is entered
    # in the PARENT lifespan. See app.py for the AsyncExitStack wiring.
    return mcp


def _apply_transport_security(mcp) -> None:
    """Configure FastMCP's DNS-rebinding host check from env, defaulting to
    OFF (trusted-LAN posture). Import is local so SDKs without the module
    don't break import of this file."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception as exc:  # noqa: BLE001
        logger.info("[seren-memory] transport_security module unavailable "
                    "(%s); leaving SDK default in place", exc)
        return

    def _split(name: str) -> list[str]:
        return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]

    allowed_hosts = _split("SEREN_MCP_ALLOWED_HOSTS")
    allowed_origins = _split("SEREN_MCP_ALLOWED_ORIGINS")

    if allowed_hosts or allowed_origins:
        # Operator opted into the check with an explicit allowlist. If they
        # only gave hosts, mirror them into origins (http+https) so a stray
        # Origin header doesn't 421 them right back.
        if not allowed_origins:
            allowed_origins = [f"http://{h}" for h in allowed_hosts] + \
                              [f"https://{h}" for h in allowed_hosts]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
        logger.info("[seren-memory] MCP host check ON; allowed_hosts=%s",
                    allowed_hosts)
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
        logger.info("[seren-memory] MCP host check OFF (trusted-LAN); set "
                    "SEREN_MCP_ALLOWED_HOSTS to enable an allowlist")


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