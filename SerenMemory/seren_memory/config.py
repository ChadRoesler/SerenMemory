"""
seren_memory.config
════════════════════════════════════════════════════════════════════════

Loads seren-memory.yaml into a typed config object. Env vars override
file values (handy for Docker / systemd where you don't want to bake
secrets into a file).

Resolution order (later wins):
    1. Defaults (in this file)
    2. seren-memory.yaml (path from --config or ./seren-memory.yaml)
    3. Environment variables (SEREN_MEMORY_*)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 7420  # distinct from Seren's other ports; "memory" has no
                      # cute base-36 derivation, just a free port that's easy
                      # to remember and unlikely to collide.
    # Optional bearer token. Empty = no auth (dev / trusted LAN).
    bearer_token: str = ""


class StorageConfig(BaseModel):
    # Where chroma persists. Each tier is a collection inside this one store.
    persist_dir: str = "~/.seren-memory/chroma"

    # Collection names. Rarely need changing, but exposed for the rare case
    # of multiple memory instances sharing a store (don't do this, but you
    # could).
    short_collection: str = "seren_short"
    near_collection: str = "seren_near"
    long_collection: str = "seren_long"
    brief_collection: str = "seren_briefs"
    draft_collection: str = "seren_consolidator_drafts"

    # Embedding model. chroma's default is all-MiniLM-L6-v2 (downloaded on
    # first use, ~80MB, runs on CPU fine). Override if you want a different
    # one. None = chroma default.
    embedding_model: Optional[str] = None


class LifetimeConfig(BaseModel):
    # ShortTerm entries older than this (seconds) are eligible to age out
    # during consolidation IF not promoted. 8 days default - a week plus
    # drift. Pinned entries ignore this.
    short_term_seconds: int = 8 * 24 * 3600  # 691200

    # NearTerm entries with no explicit expires_at that the consolidator
    # judges 'long unfulfilled' get reviewed after this. Not auto-deleted -
    # reviewed. 30 days default.
    near_term_review_seconds: int = 30 * 24 * 3600


class ConsolidatorConfig(BaseModel):
    enabled: bool = True

    # Run as a background thread in the API process (simple) or expect an
    # external process to drive it via POST /consolidate/run (advanced,
    # e.g. a separate systemd unit or cron). "thread" | "external".
    mode: str = "thread"

    # How often the consolidation window opens, in seconds. ~20 hours
    # default - deliberately NOT 24, so the window drifts through the day
    # over a week and never aligns to a 'day boundary' that doesn't exist
    # for the system. See the design notes; this number is load-bearing.
    interval_seconds: int = 20 * 3600  # 72000

    # OpenAI-compatible inference endpoint for the consolidation model.
    # Could be Seren's llama-server, ollama, a remote API, whatever speaks
    # /v1/chat/completions. The consolidator does classification + light
    # summarization, so a 2B-4B model is plenty.
    model_url: str = "http://localhost:8090/v1"
    model_name: str = "default"
    # Per-call timeout for the consolidation model. Consolidation isn't
    # latency-sensitive (it runs in the background) so this can be generous.
    model_timeout_seconds: int = 120

    # Safety cap: never process more than this many short-term entries in a
    # single window. Prevents a runaway consolidation from hammering the
    # model. Remaining entries get picked up next window.
    max_entries_per_run: int = 500

    # Promotion threshold: a topic cluster needs at least this many distinct
    # short-term entries to be promoted to long-term, UNLESS a brief
    # promote_hint or a pin overrides. Tunable - this is the main 'how
    # eager is consolidation' knob.
    promote_min_evidence: int = 3

    # Before deleting aged-out short-term entries, copy them to a
    # 'pruned' collection for this many days as insurance. 0 = no safety
    # net (delete immediately). Recommended >0 until you trust the heuristic.
    pruned_safety_days: int = 14


class MemoryConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    lifetimes: LifetimeConfig = Field(default_factory=LifetimeConfig)
    consolidator: ConsolidatorConfig = Field(default_factory=ConsolidatorConfig)

    def resolved_persist_dir(self) -> Path:
        """Expand ~ and return an absolute Path, creating it if needed."""
        p = Path(os.path.expanduser(self.storage.persist_dir)).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


def _apply_env_overrides(cfg: MemoryConfig) -> MemoryConfig:
    """Override config from SEREN_MEMORY_* env vars. Only the most commonly
    deployment-varied fields - we don't mirror every field, just the ones
    you'd actually want to set per-environment (ports, tokens, model URL,
    persist dir)."""
    env = os.environ
    if v := env.get("SEREN_MEMORY_PORT"):
        cfg.server.port = int(v)
    if v := env.get("SEREN_MEMORY_HOST"):
        cfg.server.host = v
    if v := env.get("SEREN_MEMORY_BEARER_TOKEN"):
        cfg.server.bearer_token = v
    if v := env.get("SEREN_MEMORY_PERSIST_DIR"):
        cfg.storage.persist_dir = v
    if v := env.get("SEREN_MEMORY_MODEL_URL"):
        cfg.consolidator.model_url = v
    if v := env.get("SEREN_MEMORY_MODEL_NAME"):
        cfg.consolidator.model_name = v
    if v := env.get("SEREN_MEMORY_CONSOLIDATOR_ENABLED"):
        cfg.consolidator.enabled = v.lower() in ("1", "true", "yes", "on")
    return cfg


def load_config(path: Optional[str] = None) -> MemoryConfig:
    """Load config from YAML (if present) + env overrides. Missing file is
    fine - defaults + env is a valid config (that's the zero-config dev
    experience)."""
    data: dict[str, Any] = {}

    candidate = path or os.environ.get("SEREN_MEMORY_CONFIG") or "seren-memory.yaml"
    cfg_path = Path(os.path.expanduser(candidate))
    if cfg_path.is_file():
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}

    cfg = MemoryConfig(**data)
    cfg = _apply_env_overrides(cfg)
    return cfg
