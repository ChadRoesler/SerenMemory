"""
Entry point: python -m seren_memory [--config path]

Boots the FastAPI app with uvicorn using the resolved config.
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from .app import create_app
from .config import load_config


def _force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 regardless of the OS locale.

    On Windows, the console defaults to a legacy codepage (cp1252), so any
    non-Latin-1 character a service prints - an emoji, a smart quote in a
    user's note, the model's output, an arrow in a log line - raises
    UnicodeEncodeError and can take down whatever was mid-work (the
    consolidator crashed on a '->' arrow before this guard existed).

    PYTHONUTF8=1 in the service env is the primary fix; this is the in-code
    backstop for the hand-run `python -m seren_memory` case. No-op on
    platforms that are already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            # Older Python without reconfigure, or a stream that doesn't
            # support it (already-wrapped, redirected to a non-text sink).
            # The env var still covers those paths; don't fail startup.
            pass


def _maybe_inject_truststore(cfg, log=print) -> None:
    """If tls.trust_system_store is on, route Python's TLS through the OS trust
    store via the `truststore` package.

    MUST run before any SSLContext is created (before create_app -> chromadb
    embedding download, before any httpx client), so it's called at the top of
    main() right after config load.

    Gated + logged so it's never silent: a corp box opts in via config (or the
    --corp installer), a normal box never touches it. If the flag is on but
    truststore isn't installed, tell the operator exactly what to run instead
    of dying with an opaque ImportError mid-startup.
    """
    if not cfg.tls.trust_system_store:
        return
    try:
        import truststore
    except ImportError:
        log("[seren-memory] tls.trust_system_store is ON but the 'truststore' "
            "package isn't installed. Install the corp extra: "
            "pip install 'seren-memory[corp]'  (continuing with certifi "
            "defaults - outbound TLS to a corp-proxied host will likely fail).")
        return
    truststore.inject_into_ssl()
    log("[seren-memory] TLS: using OS trust store (truststore injected)")


def main() -> None:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="seren_memory",
        description="SerenMemory - three-tier LLM memory with consolidation.")
    parser.add_argument(
        "--config", "-c", default=None,
        help="Path to seren-memory.yaml (default: ./seren-memory.yaml or "
             "$SEREN_MEMORY_CONFIG, falling back to built-in defaults).")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # Must come BEFORE create_app: create_app builds the store, which can
    # trigger chromadb's embedding-model download over TLS. If we're on a
    # corp-proxied box, the trust store has to be injected first or that
    # download fails with CERTIFICATE_VERIFY_FAILED.
    _maybe_inject_truststore(cfg)
    app = create_app(cfg)

    print(f"[seren-memory] listening on {cfg.server.host}:{cfg.server.port}")
    print(f"[seren-memory] auth: "
          f"{'enabled' if cfg.server.bearer_token else 'DISABLED (no token)'}")

    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
