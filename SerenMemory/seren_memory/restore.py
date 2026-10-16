"""
seren_memory.restore
════════════════════════════════════════════════════════════════════════

Putting a snapshot back (seren_sinew.stores.restore_at_startup), Memory's
two halves of it:

    restore_if_asked(cfg)     BEFORE the store is opened (and before the
                              embedder guard reads its stamp): copy the
                              snapshot into the empty store. Asked for by
                              backup.restore_from + backup.restore_reason,
                              and by nothing else.
    replay_tombstones(...)    AFTER the store is open: what was purged since
                              the snapshot was taken is purged again, so a
                              restore never brings a forgotten thing back.

No route calls either. Startup only, empty store only.
"""
from __future__ import annotations

from typing import Any, Optional

from .config import MemoryConfig

SERVICE = "seren-memory"


def _receipts(cfg: MemoryConfig):
    return cfg.resolved_backup_dir() / SERVICE


def eidetic_store(cfg: MemoryConfig):
    """The blob store the pictures kept with entries live in
    (seren_sinew.blobs), or None when eidetic pictures are off."""
    if not cfg.eidetic.enabled:
        return None
    from seren_sinew.blobs import BlobStore
    return BlobStore(cfg.resolved_eidetic_dir(), max_bytes=cfg.eidetic.max_bytes,
                     max_total=cfg.eidetic.max_total)


def declared_stores(cfg: MemoryConfig) -> list:
    """What Memory keeps, for a snapshot and for a restore: the chroma
    persist dir (required) and the eidetic folder beside it (optional on
    a restore: a snapshot from before it existed holds none)."""
    from seren_sinew.stores import Store
    stores = [Store("memory", "chroma", str(cfg.resolved_persist_dir()),
                    "every tier: short, near, long (cores and satellites), briefs, drafts, pruned, tombstones")]
    keep = eidetic_store(cfg)
    if keep is not None:
        stores.append(keep.store("eidetic", "pictures and clips kept with entries, named by their hash"))
    return stores


def restore_if_asked(cfg: MemoryConfig, log=print) -> Optional[dict[str, Any]]:
    """None when nothing is asked. RestoreRefused when it cannot be done as
    asked (the service must not come up empty instead)."""
    if not (cfg.backup.restore_from or "").strip():
        return None
    from seren_sinew.stores import restore_at_startup
    return restore_at_startup(
        SERVICE, declared_stores(cfg),
        cfg.backup.restore_from, cfg.backup.restore_reason, _receipts(cfg),
        log=lambda m: log(f"[seren-memory] {m}"))


def replay_tombstones(cfg: MemoryConfig, store, log=print) -> list[str]:
    """Purge again whatever a restore left pending. Cleared only when every
    one was handled, so a crash in the middle is finished at the next start."""
    from seren_sinew.stores import clear_pending_tombstones, pending_tombstones
    pending = pending_tombstones(_receipts(cfg))
    replayed: list[str] = []
    for t in pending:
        tid = str(t.get("id") or "")
        if tid and store._long_row(tid) is not None:
            store.purge_long(tid, f"replayed after a restore: {t.get('reason') or 'purged before'}",
                             purge_backups=False)
            replayed.append(tid)
    if pending:
        clear_pending_tombstones(_receipts(cfg))
        log(f"[seren-memory] restore: {len(pending)} tombstone(s) checked, {len(replayed)} purged again")
    return replayed
