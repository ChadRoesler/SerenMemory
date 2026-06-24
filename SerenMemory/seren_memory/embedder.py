"""
seren_memory.embedder
════════════════════════════════════════════════════════════════════════

Everything about WHICH embedding model the store uses, and what happens when
that choice changes.

WHY THIS EXISTS
    Chroma stores vectors, not text-with-meaning. An embedding model turns
    text into vectors; semantic search compares those vectors. The model is
    chosen once and BAKED INTO the vector space - vectors from model A are
    geometrically meaningless to model B (different dimensions, different
    geometry). So:
      * picking a better/domain-specific embedder is a real, useful knob
        (config.storage.embedding_model)
      * but CHANGING it on a store that already has data silently corrupts
        recall - old vectors and new vectors live in incompatible spaces and
        /search compares their distances as if they were comparable. No error,
        just quietly wrong answers.

    This module is the guard against that silent corruption, plus the
    sanctioned door through the wall: re-embedding migration.

THE STAMP
    A sidecar JSON (.seren_store_meta.json) INSIDE the persist_dir records
    which embedder built the store. It lives in the same directory as the
    chroma data, so a backup/copy of the persist_dir carries the stamp with
    it. It needs no embedding (unlike storing it as a chroma document, which
    would force it through the very model whose identity it's recording).

THE THREE STATES (check_store_state)
    fresh    - no stamp, or stamped-but-empty: safe to (re)stamp and proceed
    match    - stamp == configured model: proceed normally
    mismatch - stamp != configured AND data exists: THE DANGEROUS CASE.
               Boot into safe-mode, surface the migration modal, do not write.

MIGRATION
    You cannot convert old vectors to the new space. But the TEXT is the
    source of truth (vectors are a derived index, like a SQL index). So
    migration = re-embed every entry's text under the new model, IN PLACE, so
    the live persist_dir never moves (no path edit, no naming scheme). Done
    entirely through chroma's API (delete + recreate collection with the new
    embedder + re-add) - never by manipulating chroma's dir as files, which
    reads stale cache (proven the hard way). A timestamped backup copy is taken
    first; on any failure the live dir is restored from it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


STAMP_FILE = ".seren_store_meta.json"

# The collections we re-embed during migration. Mirrors MemoryStore's set.
# seren_meta is intentionally absent - the stamp is a sidecar file, not a
# collection.
MIGRATED_COLLECTIONS = (
    "short", "near", "long", "briefs", "pruned", "runs", "drafts",
)


# chroma's built-in default embedder. An empty/unset config (None / "") selects
# it, AND it can also be named explicitly - so "", None, and this literal all
# denote the SAME embedder and must never read as a mismatch against each other.
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"


# ── embedding-function resolution ───────────────────────────────────────────

def resolve_embedding_function(model_name: Optional[str], device: str = "cpu") -> Any:
    """Turn the config string into a chroma embedding-function object.

    None / "" -> None, which tells chroma to use its built-in default
    (all-MiniLM-L6-v2). A non-empty name builds a SentenceTransformer EF.

    The import is lazy so a default-config deployment never imports
    sentence-transformers (it's pulled by chroma only when actually needed).
    """
    if not model_name:
        return None
    from chromadb.utils import embedding_functions as ef
    return ef.SentenceTransformerEmbeddingFunction(model_name=model_name, device=device)


def model_label(model_name: Optional[str]) -> str:
    """Human-readable label for a model (for logs + the migration modal)."""
    return model_name if model_name else f"{DEFAULT_MODEL_NAME} (chroma default)"


# ── the stamp (sidecar JSON in the persist_dir) ─────────────────────────────

def read_stamp(persist_dir: Path) -> Optional[dict]:
    """Return the stamp dict, or None if the store was never stamped (fresh).

    Tolerant of a corrupt/partial file: a stamp we can't parse is treated as
    absent (the worst that does is re-stamp a fresh store)."""
    p = Path(persist_dir) / STAMP_FILE
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def write_stamp(persist_dir: Path, model_name: Optional[str]) -> None:
    """Record which embedder built this store. '' = chroma default.

    Written at first store creation and after a successful migration.
    """
    p = Path(persist_dir) / STAMP_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "embedding_model": model_name or "",
        "stamped_at": time.time(),
        "schema_version": 1,
    }, indent=2), encoding="utf-8")


# ── the guard ───────────────────────────────────────────────────────────────

def _canonical_model(model_name: Optional[str]) -> str:
    """Collapse the ways of naming ONE embedder to a single comparison key.

    Empty/None means "use chroma's default"; the literal default name means the
    same embedder. Both canonicalize to "" so an empty config and an explicit
    'all-MiniLM-L6-v2' stamp (or the reverse) compare EQUAL and never trip the
    guard. A genuinely different model returns its own name and still trips it.

    (Footnote, filed: "" resolves to chroma's ONNX-quantized MiniLM EF while the
    explicit name builds the sentence-transformers MiniLM EF - same model, same
    384 dims, vectors near-identical bar quantization. Treating them as one is
    correct for THIS guard, whose job is catching INCOMPATIBLE spaces, not
    distinguishing two builds of the same compatible one.)
    """
    m = (model_name or "").strip()
    return "" if m == DEFAULT_MODEL_NAME else m


def check_store_state(stamp: Optional[dict], configured_model: Optional[str],
                      has_data: bool) -> str:
    """Return 'fresh' | 'match' | 'mismatch'.

    fresh    - no stamp, or stamped but empty: caller stamps + proceeds.
    match    - stamp's model == configured model (after canonicalizing the
               default): proceed normally.
    mismatch - stamped, configured differs, AND data exists: the dangerous
               case. Caller boots safe-mode + surfaces the migration modal.

    Comparison goes through _canonical_model so "" / None / the explicit default
    name are all the SAME embedder - an empty config against a store stamped
    'all-MiniLM-L6-v2' (or vice-versa) is a match, not a false migration prompt.
    """
    if stamp is None:
        return "fresh"
    stamped = stamp.get("embedding_model", "")
    if _canonical_model(stamped) == _canonical_model(configured_model):
        return "match"
    if not has_data:
        return "fresh"
    return "mismatch"


# ── migration engine ────────────────────────────────────────────────────────

class MigrationProgress:
    """Thread-safe-enough progress holder the API polls for the loading bar.

    Single writer (the migration thread), many readers (status polls). Python
    attribute assignment is atomic for these simple types, and we never need a
    read to be consistent across multiple fields mid-write, so no lock needed.
    """
    def __init__(self) -> None:
        self.state: str = "idle"      # idle|running|done|error
        self.total: int = 0
        self.done: int = 0
        self.from_model: str = ""
        self.to_model: str = ""
        self.error: Optional[str] = None
        # In the in-place design the live persist_dir never changes, so there's
        # no "new" dir to report. stash_dir is the timestamped backup copy of
        # the pre-migration store (kept forever as the rollback).
        self.new_persist_dir: Optional[str] = None  # retained for back-compat; == live dir
        self.stash_dir: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0 if self.state in ("idle", "running") else 100
        return int(100 * self.done / self.total)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "percent": self.percent,
            "total": self.total,
            "done": self.done,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "error": self.error,
            "stash_dir": self.stash_dir,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def migrate_store(persist_dir: Path,
                  old_model: Optional[str], new_model: Optional[str],
                  collection_names: dict[str, str],
                  progress: MigrationProgress,
                  device: str = "cpu") -> None:
    """Re-embed every entry into the new embedder's space, IN PLACE.

    The live persist_dir keeps its path the whole time (so the operator never
    has to edit `storage.persist_dir` and there's no migrated-dir naming scheme
    to invent). Mechanism, proven the only safe one in testing:

      1. BACKUP: copy the live dir to <persist_dir>_YYYYMMDDHHMMSS (insurance,
         kept forever as the rollback; only ever read on a restore).
      2. READ ALL collections' docs+metadata INTO MEMORY first (shortest window
         where any collection is missing its vectors).
      3. REBUILD each collection THROUGH CHROMA'S API: delete_collection ->
         get_or_create_collection(new_ef) -> add(docs). Chroma bakes the EF in
         at create time, so this is how you change embedder for a collection.
         All operations go through the API - we never manipulate chroma's files
         as a plain tree (renaming/copying a live store's dir reads stale cache;
         this was proven the hard way - the API path is the only reliable one).
      4. STAMP LAST: write the new model to the sidecar only after every
         collection is rebuilt. A crash before this leaves the next-boot guard
         to catch the half-state (stamp still says old model).
      5. VERIFY: reopen fresh and confirm a sample vector embeds under the new
         model. On ANY failure, restore the live dir from the backup copy so
         the operator is exactly where they started - never half-migrated.

    `progress.new_persist_dir` is set to the (unchanged) live path for
    back-compat; `progress.stash_dir` is the timestamped backup.
    """
    import chromadb
    import shutil
    import gc
    from chromadb.config import Settings

    live = Path(persist_dir)
    backup = live.parent / (live.name + "_" + time.strftime("%Y%m%d%H%M%S"))

    progress.state = "running"
    progress.from_model = model_label(old_model)
    progress.to_model = model_label(new_model)
    progress.started_at = time.time()

    try:
        # 1. BACKUP (insurance). Plain copy of an idle store; only ever read
        #    back on a restore. (Reading it as files is safe; we never reopen
        #    it as a live chroma store unless restoring.)
        shutil.copytree(live, backup)
        progress.stash_dir = str(backup)

        old_ef = resolve_embedding_function(old_model, device)
        new_ef = resolve_embedding_function(new_model, device)

        # The concrete chroma collection names. Configurable ones from config;
        # fixed ones are literals (match MemoryStore).
        names = [
            collection_names.get("short_collection", "seren_short"),
            collection_names.get("near_collection", "seren_near"),
            collection_names.get("long_collection", "seren_long"),
            collection_names.get("brief_collection", "seren_briefs"),
            collection_names.get("draft_collection", "seren_consolidator_drafts"),
            "seren_pruned",
            "seren_consolidator_runs",
        ]

        client = chromadb.PersistentClient(
            path=str(live), settings=Settings(anonymized_telemetry=False))

        # 2. READ ALL into memory first (shortest missing-vectors window).
        #    We open each collection WITHOUT specifying old_ef here because
        #    .get(include=["documents","metadatas"]) is pure SQL and never
        #    touches the embedding function.  Passing old_ef to get_collection
        #    can raise an EF-mismatch exception in newer ChromaDB (Rust backend)
        #    when the stored EF config doesn't match the object we pass, which
        #    causes the collection to be silently skipped and migrated as empty.
        in_mem: dict[str, tuple] = {}
        total = 0
        for cname in names:
            try:
                col = client.get_collection(cname)
            except Exception:  # noqa: BLE001 - collection may not exist
                continue
            got = col.get(include=["documents", "metadatas"])
            ids = got.get("ids", []) or []
            in_mem[cname] = (ids,
                             got.get("documents", []) or [],
                             got.get("metadatas", []) or [])
            total += len(ids)
        progress.total = total

        # Sanity: if chroma has any of our collections but we read zero entries,
        # something went wrong in the read phase.  Abort so the backup restore
        # fires rather than silently wiping all data.
        existing_names = {c.name for c in client.list_collections()}
        our_names = set(names) & existing_names
        if our_names and total == 0:
            raise RuntimeError(
                "read phase returned 0 entries across all collections "
                f"({sorted(our_names)}); aborting to preserve backup"
            )

        # 3. REBUILD each via API: delete -> recreate(new_ef) -> re-add.
        ef_kwargs = {"embedding_function": new_ef} if new_ef is not None else {}
        for cname, (ids, docs, metas) in in_mem.items():
            try:
                client.delete_collection(cname)
            except Exception:  # noqa: BLE001
                pass
            new_col = client.get_or_create_collection(cname, **ef_kwargs)
            BATCH = 256
            for i in range(0, len(ids), BATCH):
                new_col.add(ids=ids[i:i + BATCH],
                            documents=docs[i:i + BATCH],
                            metadatas=metas[i:i + BATCH])
                progress.done += len(ids[i:i + BATCH])

        # 4. STAMP LAST.
        write_stamp(live, new_model)

        # Release the client before the verify reopen (fresh read, no cache).
        del client
        gc.collect()
        time.sleep(0.05)

        # 5. VERIFY through a fresh open: a sample must embed under new_ef.
        #    Only meaningful if we actually moved data and have a real model to
        #    compare dimension against; with the chroma-default EF we can't
        #    introspect a target dim, so we trust the API round-trip there.
        if total > 0 and new_ef is not None and in_mem:
            sample_name = next(iter(in_mem))
            vc = chromadb.PersistentClient(
                path=str(live), settings=Settings(anonymized_telemetry=False))
            try:
                scol = vc.get_collection(sample_name, embedding_function=new_ef)
                sres = scol.get(limit=1, include=["embeddings"])
                semb = sres.get("embeddings")
                # Compare against what new_ef produces for a probe string. If
                # the store still holds old-space vectors, the lengths differ.
                probe_dim = len(new_ef(["__seren_probe__"])[0])
                got_dim = len(semb[0]) if semb is not None and len(semb) > 0 else None
                if got_dim is not None and got_dim != probe_dim:
                    raise RuntimeError(
                        f"post-rebuild verify failed: stored dim {got_dim} != "
                        f"new-model dim {probe_dim}")
            finally:
                del vc
                gc.collect()

        progress.new_persist_dir = str(live)  # unchanged; for back-compat
        progress.finished_at = time.time()
        progress.state = "done"

    except Exception as e:  # noqa: BLE001
        # RESTORE: nuke the (possibly half-rebuilt) live dir, copy the backup
        # back. The operator ends exactly where they started.
        restore_note = "restored from backup; live intact"
        try:
            if live.exists():
                shutil.rmtree(live)
            shutil.copytree(backup, live)
        except Exception as restore_err:  # noqa: BLE001
            progress.error = (
                f"MIGRATION FAILED ({type(e).__name__}: {e}) AND RESTORE FAILED "
                f"({restore_err}); the untouched backup is at {backup}")
            progress.finished_at = time.time()
            progress.state = "error"
            return
        progress.error = f"{type(e).__name__}: {e} ({restore_note})"
        progress.finished_at = time.time()
        progress.state = "error"