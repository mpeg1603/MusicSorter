"""A small sqlite key/value store, so re-planning skips tag reading and repeat web lookups."""

from __future__ import annotations

import json
import os
import sqlite3


def default_cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "musicsorter", "cache.sqlite")


class Cache:
    def __init__(self, path: str | None):
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path or ":memory:")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (ns, key))"
        )
        self.pending = 0

    def get(self, ns: str, key: str) -> tuple[bool, object]:
        """Return (found, value). A stored None means 'looked up, nothing found'."""
        row = self.db.execute("SELECT value FROM kv WHERE ns = ? AND key = ?", (ns, key)).fetchone()
        return (True, json.loads(row[0])) if row else (False, None)

    def put(self, ns: str, key: str, value: object) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (ns, key, json.dumps(value)))
        self.pending += 1
        if self.pending >= 500:
            self.commit()

    def commit(self) -> None:
        self.db.commit()
        self.pending = 0

    def close(self) -> None:
        self.commit()
        self.db.close()
