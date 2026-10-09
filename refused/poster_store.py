"""SQLite persistence for POSTER_REFRESH. Blocking; call through to_thread
(except ``open``/``load_all`` at startup)."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

Key = tuple[str, str]  # (item_id, image_type)


@dataclass
class Rec:
    orig_tag: str
    fp: str | None = None        # sha256 of the image bytes; None = not baselined yet
    version: int = 0             # number of accepted changes
    changed_at: float = 0.0
    next_check: float = 0.0
    fail_count: int = 0
    last_checked: float = 0.0

    def row(self, key: Key) -> tuple:
        return (key[0], key[1], self.orig_tag, self.fp, self.version, self.changed_at,
                self.next_check, self.fail_count, self.last_checked)


class PosterStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS poster ("
            " item_id TEXT, image_type TEXT, orig_tag TEXT, fp TEXT,"
            " version INTEGER, changed_at REAL, next_check REAL,"
            " fail_count INTEGER, last_checked REAL,"
            " PRIMARY KEY (item_id, image_type))"
        )
        self._con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self._con.commit()

    def load_all(self) -> tuple[dict[Key, Rec], int]:
        with self._lock:
            records = {
                (r[0], r[1]): Rec(orig_tag=r[2], fp=r[3], version=r[4], changed_at=r[5],
                                  next_check=r[6], fail_count=r[7], last_checked=r[8])
                for r in self._con.execute(
                    "SELECT item_id, image_type, orig_tag, fp, version, changed_at,"
                    " next_check, fail_count, last_checked FROM poster"
                )
            }
            row = self._con.execute("SELECT v FROM meta WHERE k = 'epoch'").fetchone()
        return records, int(row[0]) if row else 0

    def upsert(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self._con.executemany("INSERT OR REPLACE INTO poster VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._con.commit()

    def set_epoch(self, epoch: int) -> None:
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('epoch', ?)", (str(epoch),))
            self._con.commit()

    def clear(self) -> None:
        with self._lock:
            self._con.execute("DELETE FROM poster")
            self._con.execute("DELETE FROM meta WHERE k = 'epoch'")
            self._con.commit()

    def close(self) -> None:
        with self._lock:
            self._con.close()
