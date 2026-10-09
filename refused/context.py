"""Shared application state, passed around as ``request.app.state.ctx``."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .config import Config
from .events import EventLog
from .settings import SettingsStore

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from .auth import Auth
    from .poster import PosterModule


class Ctx:
    def __init__(self, config: Config, settings: SettingsStore, events: EventLog):
        self.config = config
        self.settings = settings
        self.events = events
        self.client: "httpx.AsyncClient" = None  # type: ignore[assignment]  # set in lifespan
        self.poster: "PosterModule" = None  # type: ignore[assignment]
        self.auth: "Auth" = None  # type: ignore[assignment]
        self.started_at = time.time()
        self.strip_stats = {
            "responses_stripped": 0,
            "streams_stripped": 0,
            "fields_removed": 0,
        }
        self.diag_stats = {"requests_logged": 0}
        self.see_all_stats = {"requests_expanded": 0, "last_items_returned": 0, "pages_served": 0}
        self.see_all_served: dict[str, tuple[int, list]] = {}  # parentId -> (page size, follow-up pages)
        self.see_all_pending: dict[str, dict] = {}  # parentId -> poster checks parked until a page is sent
        self.search_stats = {"people_searches_hidden": 0}
        self.proxy_stats = {"requests": 0, "upstream_errors": 0}
        self.upstream_cache: tuple[float, dict] | None = None
        self._strip_cache: tuple[tuple[str, ...], frozenset[str]] | None = None

    def strip_names(self, fields: list[str]) -> frozenset[str]:
        key = tuple(fields)
        if self._strip_cache is None or self._strip_cache[0] != key:
            self._strip_cache = (key, frozenset(f.lower() for f in fields))
        return self._strip_cache[1]
