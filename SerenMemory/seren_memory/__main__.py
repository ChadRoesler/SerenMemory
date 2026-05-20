"""
Entry point: python -m seren_memory [--config path]

Boots the FastAPI app with uvicorn using the resolved config.
"""
from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="seren_memory",
        description="SerenMemory - three-tier LLM memory with consolidation.")
    parser.add_argument(
        "--config", "-c", default=None,
        help="Path to seren-memory.yaml (default: ./seren-memory.yaml or "
             "$SEREN_MEMORY_CONFIG, falling back to built-in defaults).")
    args = parser.parse_args()

    cfg = load_config(args.config)
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
