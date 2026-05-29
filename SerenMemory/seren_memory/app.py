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
    GET  /consolidator/status   - last run, recent runs, counts, config
    POST /brief                 - submit a daily brief (steers consolidation)
    POST /consolidate/run       - trigger consolidation now (manual / external mode)
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Body, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse

from .config import MemoryConfig, load_config
from .collections import MemoryStore
from .consolidator import Consolidator
from .models.schemas import DailyBrief
from .routes import short as short_routes
from .routes import near as near_routes
from .routes import long as long_routes
from .routes import search as search_routes


def create_app(config: MemoryConfig | None = None, embedding_function=None) -> FastAPI:
    cfg = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ── Startup ──
        store = MemoryStore(cfg, embedding_function=embedding_function)
        app.state.store = store
        app.state.config = cfg
        app.state.consolidator = Consolidator(store, cfg)
        print(f"[seren-memory] store ready at {cfg.resolved_persist_dir()}")
        print(f"[seren-memory] tiers: {store.counts()}")

        # Background consolidation loop (thread mode only). External mode
        # expects something to POST /consolidate/run on a schedule instead.
        stop_event = None
        if cfg.consolidator.enabled and cfg.consolidator.mode == "thread":
            import asyncio
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
                        print(f"[seren-memory] consolidation error: {e}")
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass

            app.state._consolidation_task = asyncio.create_task(consolidation_loop())

        yield

        # ── Shutdown ──
        if stop_event is not None:
            stop_event.set()
            task = getattr(app.state, "_consolidation_task", None)
            if task:
                try:
                    await task
                except Exception:  # noqa: BLE001
                    pass
        print("[seren-memory] shut down")

    app = FastAPI(
        title="SerenMemory",
        description="Three-tier LLM memory with consolidation. The Halls of Memory.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Optional bearer auth ──
    # Same trusted-LAN posture as the rest of Seren: if a token is set,
    # enforce it on everything except / and /health. If empty, no auth.
    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        token = cfg.server.bearer_token
        if token:
            public = request.url.path in ("/", "/health")
            if not public:
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ── Info routes ──
    @app.get("/")
    async def root(request: Request):
        store = request.app.state.store
        return {
            "service": "SerenMemory",
            "version": "0.1.0",
            "tiers": store.counts(),
            "consolidator": {
                "enabled": cfg.consolidator.enabled,
                "mode": cfg.consolidator.mode,
                "interval_seconds": cfg.consolidator.interval_seconds,
            },
        }

    @app.get("/health")
    async def health():
        return {"ok": True, "ts": time.time()}

    # ── The Halls viewer ──
    # Serves the introspection UI same-origin. This isn't just a debug tool -
    # it's the window into the consolidator: are memories clustering sensibly,
    # is near-term filling with stale loops, did a fact actually supersede the
    # old one. Serving it FROM the memory service (rather than opening the
    # file:// directly) also kills the CORS problem - the viewer's fetch calls
    # are same-origin, so the browser doesn't block them.
    @app.get("/viewer")
    async def viewer():
        # halls.html ships INSIDE the package (seren_memory/viewer/halls.html)
        # so it travels with the wheel - Path(__file__).parent is the package
        # dir whether running from a dev checkout or an installed site-packages.
        from pathlib import Path
        pkg_dir = Path(__file__).resolve().parent
        candidates = [
            pkg_dir / "viewer" / "halls.html",          # in-package (installed + dev)
            pkg_dir.parent / "viewer" / "halls.html",    # repo-root (older dev layout)
        ]
        html_path = next((p for p in candidates if p.is_file()), None)
        if html_path is None:
            return JSONResponse(
                {"error": "viewer not found",
                 "hint": "halls.html missing from the package; reinstall or grab it from the repo"},
                status_code=404)
        return FileResponse(html_path, media_type="text/html")

    # ── Consolidator operational status ──
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

    # ── Brief submission ──
    @app.post("/brief")
    async def submit_brief(request: Request, brief: DailyBrief = Body(...)):
        """Submit a daily brief. The main model calls this at the end of a
        cycle. The consolidator reads the latest brief to steer its next
        run. The brief is also itself a long-term candidate (a record of
        'what we worked on')."""
        store = request.app.state.store
        saved = store.add_brief(brief)
        return {"ok": True, "id": saved.id}

    # ── Manual consolidation trigger ──
    @app.post("/consolidate/run")
    async def consolidate_now(request: Request):
        """Run a consolidation pass right now. Used in 'external' mode (a
        cron/systemd timer POSTs here) or for manual testing. Runs the
        synchronous consolidation in a thread so we don't block the loop."""
        import asyncio
        report = await asyncio.to_thread(request.app.state.consolidator.run_once)
        return {"ok": True, "report": report}

    # ── Tier routes ──
    app.include_router(short_routes.router)
    app.include_router(near_routes.router)
    app.include_router(long_routes.router)
    app.include_router(search_routes.router)

    return app
