"""
seren_memory.app
════════════════════════════════════════════════════════════════════════

The FastAPI application. Wires the store, routes, optional bearer auth, and
the background consolidation loop.

ENDPOINTS:
    GET  /                      - service info + tier counts
    GET  /health                - liveness
    POST /short                 - add working memory       (free write)
    GET  /short                 - list                     (debug)
    DELETE /short/{id}          - remove                   (free)
    POST /near                  - add open loop            (free write)
    GET  /near                  - list                     (debug)
    POST /near/{id}/complete    - mark intent done
    DELETE /near/{id}           - abandon intent           (free)
    GET  /long                  - list                     (read-open)
    POST /long/{id}/forget      - flag for consolidator    (the Lacuna gate)
    POST /search                - unified ranked recall
    POST /by_topic              - association recall (exact topic-tag match, not similarity)
    GET  /consolidator/status   - last run, recent runs, counts, config
    POST /consolidate/run       - trigger consolidation now (manual / external mode)
    POST /consolidate/wake      - restart the background loop if it died (thread mode)
    POST /brief                 - submit a daily brief (steers consolidation)
    GET  /brief                 - list recent briefs (debug / viewer)
    POST /short/{id}/preserve   - mark for verbatim promotion (next cycle)
    POST /short/{id}/promote    - immediate verbatim promotion (skip cycle)
    GET  /drafts                - list consolidator drafts (model review queue)
    GET  /drafts/{id}/chain     - all attempts for a cluster (for comparison)
    POST /drafts/{id}/approve   - commit draft to long-term, archive shorts
    POST /drafts/{id}/reject    - send critique; triggers redraft or requires_selection
    POST /drafts/{id}/select    - commit best attempt when chain is requires_selection
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, AsyncExitStack
from typing import Optional

from fastapi import FastAPI, Body, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

from .config import MemoryConfig, load_config
from .collections import MemoryStore
from .consolidator import Consolidator
from .models.schemas import DailyBrief
from .routes import short as short_routes
from .routes import near as near_routes
from .routes import long as long_routes
from .routes import search as search_routes

from seren_meninges import get_version
from seren_meninges.auth import bearer_auth_middleware
from seren_meninges.viewer import render_from_dir
from seren_sinew.request_log import RequestLoggingMiddleware

# Reported version via the shared helper: the installed wheel's setuptools-scm
# metadata, falling back to the package __version__ for an editable/dev checkout
# where dist metadata may be absent. get_version never raises - a bad lookup
# yields the fallback, not a startup crash.
from . import __version__ as _fallback_version
APP_VERSION = get_version("seren-memory", fallback=_fallback_version)


def safe_mode_middleware(safe_mode_state: dict, allowed_paths: tuple):
    """Memory-only middleware: the embedder-mismatch 503 gate.

    When the store's vector space doesn't match the configured model,
    safe_mode_state["active"] is True and every path OUTSIDE allowed_paths gets a
    503 + the marker the Halls migration modal keys off. This is the one piece
    that can't fold into the shared bearer auth - it's Memory's own concern - so
    it's its own middleware, mounted OUTSIDE bearer (a safe-mode 503 should win
    over a 401: "we're migrating", not "wrong token", when the store is gated).
    Returns a class for app.add_middleware(...), mirroring
    seren_meninges.auth.bearer_auth_middleware.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    class _SafeMode(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if safe_mode_state["active"] and request.url.path not in allowed_paths:
                return JSONResponse(
                    {"error": "safe_mode",
                     "reason": "embedder_mismatch",
                     "detail": "store embedder changed; migrate or revert via /viewer"},
                    status_code=503)
            return await call_next(request)

    return _SafeMode


def create_app(config: MemoryConfig | None = None, embedding_function=None,
               _allow_store_reset: bool = False,
               embedder_mismatch: dict | None = None,
               config_path: str | None = None) -> FastAPI:
    cfg = config or load_config()
    # Resolve the inbound bearer ONCE at startup (resolve_bearer may hit the OS
    # keyring; per-request would be slow). Shared resolver, same as the rest of
    # the family; the combined middleware below reads this cached value.
    bearer = cfg.server.resolve_bearer()
    # Safe-mode: set when the startup guard found the store was built with a
    # different embedder than the config asks for. While set, memory read/write
    # routes are blocked (they'd touch an incompatible vector space); only the
    # viewer, health, and /migrate/* control endpoints are reachable.
    from .embedder import MigrationProgress
    _safe_mode = {"active": embedder_mismatch is not None,
                  "mismatch": embedder_mismatch}
    _migration_progress = MigrationProgress()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # -- Startup --
        app.state.config = cfg
        app.state._safe_mode = _safe_mode
        app.state._migration_progress = _migration_progress
        if _safe_mode["active"]:
            # Mismatch: do NOT build the store (would embed under the wrong
            # model against existing data). Come up reachable-but-gated so the
            # Halls migration modal can drive the fix.
            app.state.store = None
            app.state.consolidator = None
            print("[seren-memory] SAFE-MODE active (embedder mismatch); "
                  "memory ops disabled until migration or revert.")
            yield
            print("[seren-memory] shut down (safe-mode)")
            return
        store = MemoryStore(cfg, embedding_function=embedding_function,
                            _allow_reset=_allow_store_reset)
        app.state.store = store
        app.state.consolidator = Consolidator(store, cfg)
        print(f"[seren-memory] store ready at {cfg.resolved_persist_dir()}")
        print(f"[seren-memory] tiers: {store.counts()}")

        # -- Optional MCP server --
        # Mounted ONLY if the [mcp] extras are installed. The import is
        # inside the try block so a missing `mcp` package falls back to
        # pure-HTTP mode without crashing startup. One install option
        # (`pip install seren-memory[mcp]`) enables the MCP route at /mcp
        # alongside the existing HTTP API - same process, same port, same
        # config, one sec-approval surface.
        try:
            from .mcp.server import mount_mcp_routes
            mcp_server = mount_mcp_routes(app)
        except ImportError as exc:
            # `mcp` package not installed - pure HTTP mode. Quiet by
            # design: this is the default install path, not an error.
            mcp_server = None
            print(f"[seren-memory] MCP extras not installed; HTTP-only mode ({exc})")
        except Exception as exc:  # noqa: BLE001
            # SDK installed but mount failed (version drift, transport
            # mismatch, etc.) - log loudly but don't crash the service.
            # HTTP API stays up; operator can investigate.
            mcp_server = None
            print(f"[seren-memory] MCP mount failed: {exc!r} - continuing without MCP")

        # Background consolidation loop (thread mode only). External mode
        # expects something to POST /consolidate/run on a schedule instead.
        stop_event = None
        if cfg.consolidator.enabled and cfg.consolidator.mode == "thread":
            stop_event = asyncio.Event()
            app.state._stop_event = stop_event

            async def consolidation_loop():
                interval = cfg.consolidator.interval_seconds
                print(f"[seren-memory] consolidation loop active (every {interval}s)")
                # Warmup delay so first run doesn't collide with startup.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60)
                    return  # stopped during warmup
                except asyncio.TimeoutError:
                    pass
                while not stop_event.is_set():
                    try:
                        # Run the (synchronous) consolidation off the event loop.
                        await asyncio.to_thread(app.state.consolidator.run_once)
                    except Exception as e:  # noqa: BLE001
                        from .consolidator.service import ConsolidatorBusy
                        if isinstance(e, ConsolidatorBusy):
                            print(f"[seren-memory] scheduled tick skipped: {e}")
                        else:
                            print(f"[seren-memory] consolidation error: {e}")
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass

            def _start_loop():
                t = asyncio.create_task(consolidation_loop())
                app.state._consolidation_task = t
                return t

            _start_loop()
            # Expose the loop starter for the wake endpoint.
            app.state._start_consolidation_loop = _start_loop

        # -- Run the MCP session manager's task group (Bug 2 fix) --
        # The streamable-HTTP transport keeps its anyio task group alive in
        # session_manager.run(); without entering it here every MCP request
        # 500s with "Task group is not initialized". A mounted sub-app's own
        # lifespan does NOT fire under Starlette, so this is the only place
        # it can live. AsyncExitStack makes HTTP-only mode (mcp_server is
        # None) a clean no-op - we enter nothing and just yield.
        async with AsyncExitStack() as _mcp_stack:
            session_manager = getattr(mcp_server, "session_manager", None)
            if session_manager is not None:
                await _mcp_stack.enter_async_context(session_manager.run())
                print("[seren-memory] MCP session manager running")
            yield

        # -- Shutdown --
        if stop_event is not None:
            stop_event.set()
            task = getattr(app.state, "_consolidation_task", None)
            if task:
                try:
                    await task
                except Exception:  # noqa: BLE001
                    pass
        # Release the ChromaDB client so SQLite handles are closed cleanly.
        try:
            app.state.store.close()
        except Exception:  # noqa: BLE001
            pass
        print("[seren-memory] shut down")

    app = FastAPI(
        title="SerenMemory",
        description="Three-tier LLM memory with consolidation for Seren. The right brain - episodic, time-tiered, dreaming the durable out of the fleeting. The Halls of Memory.",
        version=APP_VERSION,
        lifespan=lifespan,
    )

    # -- Auth + safe-mode + logging stack --
    # Stack, outer->inner: logging -> safe-mode -> bearer -> routes. Register
    # inner-first (Starlette mounts LIFO): bearer, then safe-mode, then logging,
    # so a safe-mode 503 fires before bearer can 401, and logging wraps it all.
    #
    # Bearer is now FOLDED into the family - the constant-time compare + public-
    # paths policy live in SerenMeninges so all four services enforce auth
    # identically (a fix lands everywhere). The Meninges default public set is
    # exactly Memory's ({"/", "/health", "/viewer"}), so no override is needed.
    # The ONE Memory-specific concern - the embedder safe-mode 503 gate - can't
    # fold in, so it's its own middleware mounted OUTSIDE bearer (so "we're
    # migrating" / 503 wins over "wrong token" / 401 when the store is gated).
    _SAFE_MODE_ALLOWED = (
        "/", "/health", "/viewer",
        "/migrate/status", "/migrate/accept", "/migrate/deny", "/migrate/restart",
    )
    app.add_middleware(bearer_auth_middleware(bearer))                        # inner
    app.add_middleware(safe_mode_middleware(_safe_mode, _SAFE_MODE_ALLOWED))  # middle
    app.add_middleware(                                                       # outer
        RequestLoggingMiddleware,
        service_name="seren-memory",
        env_prefix="SEREN_MEMORY",
    )

    # -- Info routes --
    @app.get("/")
    async def root(request: Request):
        if _safe_mode["active"]:
            mm = _safe_mode["mismatch"] or {}
            return JSONResponse({
                "service": "SerenMemory",
                "version": APP_VERSION,
                "safe_mode": True,
                "mismatch": mm,
            }, status_code=503)
        store = request.app.state.store
        return {
            "service": "SerenMemory",
            "version": APP_VERSION,
            "tiers": store.counts(),
            "embedding_model": cfg.storage.embedding_model or "all-MiniLM-L6-v2 (default)",
            "consolidator": {
                "enabled": cfg.consolidator.enabled,
                "mode": cfg.consolidator.mode,
                "interval_seconds": cfg.consolidator.interval_seconds,
            },
        }

    @app.get("/health")
    async def health():
        return {"ok": True, "ts": time.time()}

    # -- The Halls viewer --
    # Serves the introspection UI same-origin. This isn't just a debug tool -
    # it's the window into the consolidator: are memories clustering sensibly,
    # is near-term filling with stale loops, did a fact actually supersede the
    # old one. Serving it FROM the memory service (rather than opening the
    # file:// directly) also kills the CORS problem - the viewer's fetch calls
    # are same-origin, so the browser doesn't block them.
    @app.get("/viewer")
    async def viewer():
        # The Halls - coral UI, per-tier palette, the draft-review surface and
        # the embedder safe-mode migration modal. Snaps the leaf fragment files
        # in viewer/ui/ onto the shared SerenMeninges baseplate. Public route
        # (the HTML needs no auth); its API calls carry the token. /viewer stays
        # reachable in safe-mode so the migration modal can drive the fix.
        from pathlib import Path
        html = render_from_dir(
            Path(__file__).resolve().parent / "viewer" / "ui",
            title="SerenMemory",
            brand="Seren<b>Memory</b> · Halls of Memory",
            subtitle=f"v{APP_VERSION} · episodic, consolidated, gated",
            accent="#ff6e8a",
        )
        return HTMLResponse(html)

    # -- Consolidator operational status --
    @app.get("/consolidator/status")
    async def consolidator_status(request: Request):
        """Operational snapshot: when did the consolidator last run, how did
        it go, what's the current cluster state. Backs the MCP
        get_consolidator_status tool and the Halls viewer's operational
        panel. last_run is null if the consolidator has never run on this
        deployment.
        """
        store = request.app.state.store
        return {
            "last_run": store.get_latest_run(),
            "recent_runs": store.get_recent_runs(limit=10),
            "latest_brief": store.get_latest_brief(),
            "counts": store.counts(),
            "config": {
                "enabled": cfg.consolidator.enabled,
                "mode": cfg.consolidator.mode,
                "interval_seconds": cfg.consolidator.interval_seconds,
            },
        }

    # -- Brief submission --
    @app.post("/brief")
    async def submit_brief(request: Request, brief: DailyBrief = Body(...)):
        """Submit a daily brief. The main model calls this at the end of a
        cycle. The consolidator reads the latest brief to steer its next
        run. The brief is also itself a long-term candidate (a record of
        'what we worked on')."""
        store = request.app.state.store
        saved = store.add_brief(brief)
        return {"ok": True, "id": saved.id}

    @app.get("/brief")
    async def list_briefs(request: Request, limit: int = 20):
        """List the most recent briefs, newest first. Backs the Halls
        viewer's brief panel - lets you see steering history alongside
        the tier collections."""
        store = request.app.state.store
        rows = store.get_recent_briefs(limit=limit)
        return {"entries": rows, "count": len(rows)}

    # -- Consolidator drafts (model review queue) --
    @app.get("/drafts")
    async def list_drafts(request: Request, limit: int = 20,
                          status: Optional[str] = None):
        """List consolidator drafts. Defaults to all statuses, newest first;
        pass status=pending for the active review queue, approved/rejected
        for history, or requires_selection when the redraft budget ran out.

        Each draft carries source_short_ids (the cluster evidence), attempt
        (1-based position in its redraft chain), cluster_id (shared across
        all attempts for one cluster), and previous_draft_ids so the model
        can compare the full chain before selecting.
        """
        store = request.app.state.store
        rows = store.get_recent_drafts(limit=limit, status=status)
        return {"entries": rows, "count": len(rows)}

    @app.get("/drafts/{draft_id}/chain")
    async def get_draft_chain(request: Request, draft_id: str):
        """Return all synthesis attempts for the same cluster as draft_id,
        ordered by attempt number (ascending). Use this to compare every
        draft in a chain before selecting the best one via /select.
        Returns 404 if draft_id is not found.
        """
        store = request.app.state.store
        row = store._get_draft_row(draft_id)
        if not row:
            raise HTTPException(404, f"no draft '{draft_id}'")
        cluster_id = row["metadata"].get("cluster_id", draft_id)
        chain = store.get_drafts_by_cluster(cluster_id)
        return {"cluster_id": cluster_id, "attempts": chain, "count": len(chain)}

    @app.post("/drafts/{draft_id}/approve")
    async def approve_draft(request: Request, draft_id: str,
                            body: Optional[dict] = Body(None)):
        """Approve a pending draft. Commits the synthesis to long-term and
        archives the source shorts to pruned. Optional 'note' in body is
        recorded with the approval.

        Returns 404 if draft missing, 409 if already reviewed.
        """
        store = request.app.state.store
        note = (body or {}).get("note")
        result = store.approve_draft(draft_id, note=note)
        if result is None:
            existing = store._get_draft_row(draft_id)
            if not existing:
                raise HTTPException(404, f"no draft '{draft_id}'")
            raise HTTPException(409,
                f"draft '{draft_id}' already {existing['metadata'].get('status')}")
        return {
            "ok": True,
            "draft_id": draft_id,
            "long_term_id": result["long_term_id"],
            "shorts_archived": result["shorts_archived"],
        }

    @app.post("/drafts/{draft_id}/reject")
    async def reject_draft(request: Request, draft_id: str, body: dict = Body(...)):
        """Reject a pending draft with a critique. The consolidator will
        produce a new synthesis incorporating the critique (up to
        max_redraft_attempts total tries). Once the limit is exhausted the
        chain flips to requires_selection and the model must POST /select.

        Body: {"critique": "<why this synthesis is wrong/incomplete>"}
        Legacy key 'reason' is accepted as an alias.

        Returns 400 if no critique, 404 if draft missing, 409 if already
        reviewed. Response includes action ('redrafted' or
        'requires_selection') and, when redrafting, the new draft_id.
        """
        critique = (body or {}).get("critique") or (body or {}).get("reason", "")
        critique = critique.strip() if critique else ""
        if not critique:
            raise HTTPException(400, "a 'critique' is required to reject a draft")
        store = request.app.state.store
        cluster_meta = store.reject_draft(draft_id, critique)
        if cluster_meta is None:
            existing = store._get_draft_row(draft_id)
            if not existing:
                raise HTTPException(404, f"no draft '{draft_id}'")
            raise HTTPException(409,
                f"draft '{draft_id}' already {existing['metadata'].get('status')}")

        # Trigger redraft (or flip to requires_selection) on the consolidator.
        consolidator = request.app.state.consolidator
        redraft_result = await asyncio.to_thread(
            consolidator.redraft_cluster,
            cluster_id=cluster_meta["cluster_id"],
            rejected_draft_id=draft_id,
            critique=critique,
            attempt=cluster_meta["attempt"],
            source_short_ids=cluster_meta["source_short_ids"],
            brief_id_used=cluster_meta["brief_id_used"],
            topic=cluster_meta["topic"],
            evidence_count=cluster_meta["evidence_count"],
        )
        if redraft_result is None:
            return {
                "ok": True, "draft_id": draft_id,
                "action": "rejected", "critique": critique,
                "warning": "redraft synthesis failed; cluster stays in pool",
            }
        return {
            "ok": True,
            "draft_id": draft_id,
            "action": redraft_result["action"],
            "critique": critique,
            "new_draft_id": redraft_result.get("draft_id"),
            "attempt": redraft_result["attempt"],
        }

    @app.post("/drafts/{draft_id}/select")
    async def select_draft(request: Request, draft_id: str,
                           body: Optional[dict] = Body(None)):
        """Commit the best attempt from a requires_selection chain to
        long-term. The selected draft is approved and all sibling attempts
        are marked rejected. Source shorts are archived to pruned.

        Body (optional):
            {
              "edited_content": "<revised text to commit instead of the draft>",
              "note": "<freeform note on the selection>"
            }

        edited_content is the editor's safety valve: when all redraft
        attempts are unsatisfactory, the editor picks the best of the chain
        and can revise it before commit. If edited_content is omitted (or
        None), the draft commits as-is. The original draft's content is
        preserved on the draft row (and copied into the long-term entry's
        extra dict) so the audit trail stays intact - we can always answer
        "what did the consolidator originally synthesize" even after edit.

        Edit is only available on this path (not on approve), which keeps
        the iteration loop discipline: if you want to tweak during the loop,
        reject with a critique and let the consolidator re-synthesize. Edit
        is the terminal-state release valve, not a shortcut.

        Call GET /drafts/{id}/chain first to compare all attempts, then
        POST /select on the one judged best (optionally with edits).

        Returns 400 if edited_content is provided as blank/whitespace,
        404 if draft missing, 409 if not in requires_selection state.
        Response includes 'edited' (bool) and 'edit_delta_chars' (int) so
        the operator can see the magnitude of any revision at a glance.
        """
        body = body or {}
        edited_content = body.get("edited_content")
        note = body.get("note")

        # Empty edits are a bug, not "no edit". Reject explicitly so the
        # editor can't accidentally commit a blank long-term entry.
        if edited_content is not None:
            if not isinstance(edited_content, str) or not edited_content.strip():
                raise HTTPException(
                    400,
                    "edited_content must be a non-empty string; omit the field "
                    "to commit the draft as-is")

        store = request.app.state.store
        result = store.select_draft(draft_id, note=note,
                                    edited_content=edited_content)
        if result is None:
            existing = store._get_draft_row(draft_id)
            if not existing:
                raise HTTPException(404, f"no draft '{draft_id}'")
            status = existing["metadata"].get("status")
            raise HTTPException(409,
                f"draft '{draft_id}' is '{status}', not requires_selection")
        return {
            "ok": True,
            "draft_id": draft_id,
            "long_term_id": result["long_term_id"],
            "shorts_archived": result["shorts_archived"],
            "edited": result["edited"],
            "edit_delta_chars": result["edit_delta_chars"],
        }

    # -- Short-term agency endpoints (preserve_verbatim + promote_memory) --
    @app.post("/short/{entry_id}/preserve")
    async def preserve_short_verbatim(request: Request, entry_id: str):
        """Mark a short-term entry to be promoted VERBATIM (no synthesis,
        no fusion) on the next consolidator cycle. Also pins it so it
        survives aging until the cycle runs. Returns 404 if not found."""
        store = request.app.state.store
        ok = store.update_short_metadata(entry_id, {"verbatim": True, "pinned": True})
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"short-term entry '{entry_id}' not found")
        return {"ok": True, "id": entry_id, "verbatim": True, "pinned": True}

    @app.post("/short/{entry_id}/promote")
    async def promote_short_immediately(request: Request, entry_id: str):
        """Immediately move a short-term entry to long-term verbatim,
        bypassing the consolidator cycle entirely. The 'I know this is
        durable, don't make me wait' override. Returns 404 if not found."""
        store = request.app.state.store
        long_id = store.promote_short_to_long(entry_id)
        if long_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"short-term entry '{entry_id}' not found")
        return {"ok": True, "long_term_id": long_id, "removed_short_id": entry_id}

    # -- Manual consolidation trigger + wake --
    @app.post("/consolidate/run")
    async def consolidate_now(request: Request):
        """Run a consolidation pass right now. Used in 'external' mode (a
        cron/systemd timer POSTs here) or for manual testing. Runs the
        synchronous consolidation in a thread so we don't block the loop.

        Returns 409 if a scheduled run is already in progress.
        """
        from .consolidator.service import ConsolidatorBusy
        try:
            report = await asyncio.to_thread(
                request.app.state.consolidator.run_once)
        except ConsolidatorBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "report": report}

    @app.post("/consolidate/wake")
    async def wake_consolidator(request: Request):
        """Restart the background consolidation loop if it has died or is not
        running. No-op when mode is 'external' (there is no loop to wake).
        Returns 'already_running' if the task is still alive, 'woken' if it
        was restarted, or 'not_applicable' in external mode.
        """
        cfg = request.app.state.config
        if not cfg.consolidator.enabled or cfg.consolidator.mode != "thread":
            return {"ok": True, "status": "not_applicable",
                    "detail": "consolidator is in external mode; POST /consolidate/run to trigger"}

        task: Optional[asyncio.Task] = getattr(request.app.state,
                                               "_consolidation_task", None)
        if task is not None and not task.done():
            return {"ok": True, "status": "already_running"}

        starter = getattr(request.app.state, "_start_consolidation_loop", None)
        if starter is None:
            return {"ok": False, "status": "error",
                    "detail": "loop starter not available; restart the service"}

        starter()
        return {"ok": True, "status": "woken",
                "detail": "background consolidation loop restarted"}

    # -- Migration control (drives the Halls 'embedder changed' modal) --
    @app.get("/migrate/status")
    async def migrate_status(request: Request):
        """Current safe-mode + migration progress. The modal polls this for
        the loading bar and to know when to redirect back to the Halls.
        `supervised` tells the UI whether the restart button can actually
        bounce the service or should show manual instructions instead."""
        from ._supervised import detect_supervised
        return {
            "safe_mode": _safe_mode["active"],
            "mismatch": _safe_mode["mismatch"],
            "migration": _migration_progress.snapshot(),
            "supervised": detect_supervised()["supervised"],
        }

    @app.post("/migrate/accept")
    async def migrate_accept(request: Request):
        """Accept: re-embed the store into a NEW persist_dir under the
        configured model, on a background thread. Old dir is left as rollback.
        On success the operator restarts pointed at the new dir (the response
        tells them where). 409 if not in safe-mode or already running."""
        import asyncio
        from pathlib import Path
        from .embedder import migrate_store, MigrationProgress
        if not _safe_mode["active"]:
            raise HTTPException(409, "not in safe-mode; nothing to migrate")
        if _migration_progress.state == "running":
            raise HTTPException(409, "migration already running")

        mm = _safe_mode["mismatch"] or {}
        live_dir = cfg.resolved_persist_dir()

        coll_names = {
            "short_collection": cfg.storage.short_collection,
            "near_collection": cfg.storage.near_collection,
            "long_collection": cfg.storage.long_collection,
            "brief_collection": cfg.storage.brief_collection,
            "draft_collection": cfg.storage.draft_collection,
        }

        # Reset progress and run migrate_store off the event loop. IN-PLACE:
        # migrate_store re-embeds the LIVE dir (no path change) and keeps a
        # timestamped backup as rollback. Args are POSITIONAL and must match
        # embedder.migrate_store(persist_dir, old_model, new_model,
        # collection_names, progress[, device]).
        prog = app.state._migration_progress
        prog.__init__()  # reset to idle
        asyncio.get_event_loop().run_in_executor(
            None, migrate_store,
            live_dir,                              # persist_dir (live, in place)
            mm.get("stamped_model") or None,       # old_model
            mm.get("configured_model") or None,    # new_model
            coll_names,                            # collection_names
            prog)                                  # progress
        return {"ok": True, "started": True,
                "note": "Migration running in place (live store re-embedded, "
                        "timestamped backup kept). Poll /migrate/status; when "
                        "done, restart to load the migrated store."}

    @app.post("/migrate/deny")
    async def migrate_deny(request: Request):
        """Deny: revert the config's embedding_model back to the stamped value
        so the mismatch is resolved permanently, then ask the operator to
        restart. We rewrite the yaml if we know its path; otherwise we return
        the value to set. 409 if not in safe-mode."""
        import yaml
        from pathlib import Path
        if not _safe_mode["active"]:
            raise HTTPException(409, "not in safe-mode; nothing to revert")
        mm = _safe_mode["mismatch"] or {}
        stamped = mm.get("stamped_model") or ""
        target = stamped or None  # '' default -> null in yaml

        rewritten = False
        if config_path:
            p = Path(config_path).expanduser()
            if p.is_file():
                data = yaml.safe_load(p.read_text()) or {}
                data.setdefault("storage", {})
                data["storage"]["embedding_model"] = target
                p.write_text(yaml.safe_dump(data, default_flow_style=False,
                                            sort_keys=False))
                rewritten = True
        return {
            "ok": True,
            "reverted_to": target,
            "config_rewritten": rewritten,
            "detail": ("Config reverted; restart the service to resume normally."
                       if rewritten else
                       "No writable config path known; set storage.embedding_model "
                       f"to {target!r} yourself, then restart."),
        }

    @app.post("/migrate/restart")
    async def migrate_restart(request: Request):
        """Restart the service so a migrated/reverted store loads. GATED: only
        self-exits when we're confident a manager will bring us back (our
        SEREN_SUPERVISED flag, or systemd's INVOCATION_ID). Otherwise returns
        manual instructions - never kill a process with nothing to revive it.

        The exit is scheduled AFTER the response is sent (a background task on
        a short delay), so the HTTP call completes before the process goes.
        """
        from ._supervised import detect_supervised
        import os, sys, asyncio, platform

        sup = detect_supervised()
        if not sup["supervised"]:
            # Build a best-effort manual hint for the platform.
            if platform.system() == "Windows":
                hint = "Restart the SerenMemory service (e.g. `nssm restart SerenMemory`), or stop and re-run it."
            else:
                hint = "Restart the service: `sudo systemctl restart seren-memory` (or re-run however you started it)."
            return {
                "ok": True,
                "action": "manual_restart_required",
                "detail": "Migration is complete, but this process isn't under "
                          "a manager that auto-restarts it. Restart SerenMemory "
                          "yourself to load the migrated store.",
                "hint": hint,
                "signals": sup["signals"],
            }

        async def _exit_soon():
            # Let the response flush, then exit. The manager restarts us.
            await asyncio.sleep(0.5)
            # os._exit avoids running atexit/cleanup that could hang; the
            # manager's restart policy handles bringing us back fresh.
            os._exit(0)

        asyncio.create_task(_exit_soon())
        return {
            "ok": True,
            "action": "restarting",
            "detail": "Service is supervised; exiting now so the manager "
                      "restarts it with the migrated store.",
        }

    # -- Pointer dereference (hydrate one memory by id) --
    @app.get("/memory/{entry_id}")
    async def get_memory(request: Request, entry_id: str):
        """Hydrate a single memory by id across the recall tiers. The
        dereference for a pointer handed back by /search: search gives you
        the id, this returns the whole entry (content + metadata + which
        tier it lives in) so an idea can be inspected closer or its
        surrounding context pulled, instead of re-searching. 404 if no
        recall tier holds that id. Read-only; gated in safe-mode like any
        recall."""
        store = request.app.state.store
        row = store.get_by_id(entry_id)
        if row is None:
            raise HTTPException(404, f"no memory '{entry_id}' in short/near/long")
        return {"ok": True, **row}

    # -- Distill reconcile (re-embed existing entries onto the distilled key) --
    @app.post("/reconcile/distill")
    async def reconcile_distill(request: Request, apply: bool = False):
        """Re-embed existing short+long entries onto the distilled retrieval key
        (see MemoryStore.reconcile_distill_keys). The retroactive half of
        distill: add_short/add_long embed a topic-anchored key for NEW writes;
        this fixes entries written before that landed so old blobs stop out-
        cosining one-line facts. Lossless (only the vector changes; documents +
        metadata untouched), idempotent, same embedder. DRY-RUN by default -
        POST ?apply=true to commit. Take a persist_dir backup first; the op is
        non-destructive but backup is cheap insurance. Runs in-process (off the
        event loop) so there's no dual-client conflict with the live service."""
        store = request.app.state.store
        report = await asyncio.to_thread(
            store.reconcile_distill_keys, dry_run=not apply)
        return {"ok": True, **report}

    # -- Tier routes --
    app.include_router(short_routes.router)
    app.include_router(near_routes.router)
    app.include_router(long_routes.router)
    app.include_router(search_routes.router)

    return app