"""Small in-memory ring buffer feeding the LOG tab of the admin panel."""
from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger("refused")

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


class EventLog:
    def __init__(self, maxlen: int = 1500):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._next_id = 1

    def add(self, level: str, cat: str, msg: str) -> None:
        level = level.upper()
        self._buf.append(
            {
                "id": self._next_id,
                "ts": time.time(),
                "level": level,
                "cat": cat,
                "msg": msg,
            }
        )
        self._next_id += 1
        log.log(_LEVELS.get(level, 20), "[%s] %s", cat, msg)

    def info(self, cat: str, msg: str) -> None:
        self.add("INFO", cat, msg)

    def warn(self, cat: str, msg: str) -> None:
        self.add("WARN", cat, msg)

    def error(self, cat: str, msg: str) -> None:
        self.add("ERROR", cat, msg)

    def since(self, after: int = 0, limit: int = 400) -> list[dict]:
        out = [e for e in self._buf if e["id"] > after]
        return out[-limit:]

    @property
    def last_id(self) -> int:
        return self._next_id - 1
