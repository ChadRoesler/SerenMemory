"""
seren_memory.hits
════════════════════════════════════════════════════════════════════════

The recall ledger for satellites: how often each one was the episode that
brought its core into a packet (the hit's `matched_via`), and when last.

WHY A SIDE TABLE AND NOT THE ROW. The lifecycle design (21 Oct 2026): a
satellite that keeps getting attention is a core wearing a satellite's
coat, and should be promoted out of the glitter. Noticing that needs a
signal, and the store recorded no recall per entry. A counter on the row
itself would make every search a metadata write on the vector collection
(and a vector collection is the wrong place for a number that changes on
every read); a small JSON beside the store, written atomically, costs one
file write per search that lifted a core through an episode and nothing
otherwise. It lives in the persist dir, so a snapshot carries it.

What counts: a satellite named as `matched_via` on a core that made the
FINAL packet (after ranking and the trim), once per search however many
cores it lifted. An orbiting short is not a satellite and is not counted
here (it ages out or is promoted on its own path). A satellite that is a
core's own earlier wording (was_core) or its kept episode is counted like
any other, and the hot list leaves those out: promoting a core's own words
would make the core twice.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


class HitLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._load()

    # -- the file --------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._rows = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self._rows = {}
        except (OSError, ValueError):
            # a half-written or corrupt file starts the count over; the memories themselves are untouched
            self._rows = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._rows, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)       # atomic overwrite on every platform; .rename raises on Windows when the target exists

    # -- counting --------------------------------------------------------------
    def bump(self, ids: Iterable[str], now: Optional[float] = None) -> int:
        """One hit for each satellite named, once per call. Returns how many were counted."""
        now = now or time.time()
        counted = 0
        with self._lock:
            for sid in dict.fromkeys(str(s) for s in ids if s):
                row = self._rows.setdefault(sid, {"n": 0, "first": now})
                row["n"] = int(row.get("n", 0) or 0) + 1
                row["last"] = now
                counted += 1
            if counted:
                self._save()
        return counted

    def get(self, sid: str) -> Optional[dict[str, Any]]:
        row = self._rows.get(str(sid))
        return dict(row) if row else None

    def count(self, sid: str) -> int:
        row = self._rows.get(str(sid))
        return int(row.get("n", 0) or 0) if row else 0

    def hot(self, min_hits: int = 1, limit: int = 50) -> list[tuple[str, dict[str, Any]]]:
        """Satellites at or past min_hits, most recalled first, then most recent."""
        rows = [(sid, dict(r)) for sid, r in self._rows.items() if int(r.get("n", 0) or 0) >= max(1, int(min_hits))]
        rows.sort(key=lambda kv: (-int(kv[1].get("n", 0) or 0), -float(kv[1].get("last", 0) or 0)))
        return rows[: max(0, int(limit))]

    # -- the row is gone, or became another ------------------------------------
    def forget(self, ids: Iterable[str]) -> int:
        """A purged satellite's count goes with it."""
        gone = 0
        with self._lock:
            for sid in ids:
                if self._rows.pop(str(sid), None) is not None:
                    gone += 1
            if gone:
                self._save()
        return gone

    def merge(self, dup_id: str, into_id: str) -> None:
        """A fold: two satellites that were one episode. Their counts add up on the one kept."""
        with self._lock:
            dup = self._rows.pop(str(dup_id), None)
            if not dup:
                return
            into = self._rows.setdefault(str(into_id), {"n": 0, "first": dup.get("first")})
            into["n"] = int(into.get("n", 0) or 0) + int(dup.get("n", 0) or 0)
            into["last"] = max(float(into.get("last", 0) or 0), float(dup.get("last", 0) or 0)) or None
            if into.get("last") is None:
                into.pop("last", None)
            into["first"] = min(float(into.get("first") or dup.get("first") or 0), float(dup.get("first") or into.get("first") or 0)) or None
            if into.get("first") is None:
                into.pop("first", None)
            self._save()

    def __len__(self) -> int:
        return len(self._rows)
