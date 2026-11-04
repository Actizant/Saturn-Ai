"""
memory/ondemand_store.py - A disk-backed key/value store with an on-disk index
and a bounded in-RAM working set.

This is the Part 2 "framework RAM suppression" primitive, delivered as a
self-contained, opt-in module. It is deliberately NOT wired in as a replacement
for memory_manager.py: the current JSON long-term memory is capped at
MEMORY_MAX_CHARS = 200_000 (a couple hundred kilobytes), so it is not where the
RAM actually goes, and swapping its storage engine would put 600+ passing tests
at risk for no measurable benefit. This store instead gives the app a place to
put genuinely large, unbounded note/record sets -- logs, scraped pages, big
knowledge dumps -- where a Python dict would grow without limit.

The suppression contract:

  * SQLite is the source of truth; rows live on disk, not in a dict.
  * We never call a "load everything" path. Reads are top-K by relevance
    (an FTS5 index when available, a LIKE scan otherwise), so the working set
    is bounded by the caller's `limit`, not by the size of the dataset.
  * A single connection is reused; PRAGMAs cap the page cache so even SQLite's
    own buffer stays small on a 4GB machine.

If any of that ever fails (missing sqlite3, corrupt db), it raises loudly rather
than silently dropping data -- this holds user records, not caches.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("ondemand_store")

# Keep SQLite's own in-memory page cache small; on a constrained laptop the whole
# point is that nothing here gets to grow into the RAM the models need.
_CACHE_KIB = -2048       # negative => KiB, so a 2MB page-cache ceiling
_JOURNAL = "WAL"


class OnDemandStore:
    """A bounded-working-set, disk-backed record store.

    Thread-safe: a lock serializes access to the single shared connection
    (sqlite3 connections are not thread-affine here because check_same_thread is
    off, but concurrent writes still need ordering).
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._have_fts = False
        self._init_schema()

    # ------------------------------------------------------------- lifecycle ---

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"PRAGMA journal_mode={_JOURNAL}")
            cur.execute(f"PRAGMA cache_size={_CACHE_KIB}")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    category   TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            # A normal index on category keeps scoped listings cheap without FTS.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_category ON records(category)"
            )
            # Full-text index for relevance search, when this build has FTS5.
            try:
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts "
                    "USING fts5(key UNINDEXED, value, category UNINDEXED, "
                    "content='records', content_rowid='rowid')"
                )
                self._have_fts = True
            except sqlite3.OperationalError:
                self._have_fts = False
                logger.info("[STORE] FTS5 unavailable; falling back to LIKE scans")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------- crud ---

    def put(self, key: str, value: str, category: str = "") -> None:
        import time

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO records(key, value, category, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "category=excluded.category, updated_at=excluded.updated_at",
                (key, str(value), category, time.time()),
            )
            if self._have_fts:
                rowid = cur.execute("SELECT rowid FROM records WHERE key=?", (key,)).fetchone()[0]
                cur.execute(
                    "INSERT OR REPLACE INTO records_fts(rowid, key, value, category) "
                    "VALUES(?,?,?,?)",
                    (rowid, key, str(value), category),
                )
            self._conn.commit()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM records WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def delete(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            if self._have_fts:
                rowid_row = cur.execute("SELECT rowid FROM records WHERE key=?", (key,)).fetchone()
                if rowid_row:
                    cur.execute("DELETE FROM records_fts WHERE rowid=?", (rowid_row[0],))
            deleted = cur.execute("DELETE FROM records WHERE key=?", (key,)).rowcount
            self._conn.commit()
            return bool(deleted)

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    # ----------------------------------------------------------------- search ---

    def search(self, query: str, limit: int = 8, category: str = "") -> List[Dict[str, Any]]:
        """Top-K relevance results. Only `limit` rows are materialised into
        Python, so the working set stays bounded no matter how big the table is.

        Uses the FTS5 index (bm25 ranking) when present; otherwise a LIKE scan.
        Both run entirely in SQLite -- we never pull the whole table into a dict.
        """
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 64))
        with self._lock:
            if self._have_fts:
                sql = (
                    "SELECT r.key, r.value, r.category FROM records_fts f "
                    "JOIN records r ON r.rowid = f.rowid "
                    "WHERE records_fts MATCH ?"
                )
                params: List[Any] = [query]
                if category:
                    sql += " AND r.category = ?"
                    params.append(category)
                sql += " ORDER BY bm25(records_fts) LIMIT ?"
                params.append(limit)
                try:
                    rows = self._conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    rows = []  # malformed FTS query syntax -> fall through to LIKE
                if rows:
                    return [dict(r) for r in rows]
            # LIKE fallback (also the path when FTS matched nothing or errored).
            sql = "SELECT key, value, category FROM records WHERE value LIKE ?"
            params = [f"%{query}%"]
            if category:
                sql += " AND category = ?"
                params.append(category)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


_default: Optional[OnDemandStore] = None
_default_lock = threading.Lock()


def get_store(subdir: str = "ondemand") -> OnDemandStore:
    """Lazily open the process-wide store under the user data dir. Nothing calls
    this unless a feature opts in, so the default app footprint is unchanged."""
    global _default
    with _default_lock:
        if _default is None:
            from core.user_paths import get_user_data_dir

            path = get_user_data_dir() / "memory" / subdir / "ondemand.db"
            _default = OnDemandStore(path)
        return _default
