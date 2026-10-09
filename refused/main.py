"""Application factory: wires config, settings, proxy and admin panel together.

Run with:  uvicorn refused.main:create_app --factory
Run exactly one process/worker: settings, sessions and poster state are in memory.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import admin, proxy
from .auth import Auth
from .config import ADMIN_BASE, VERSION, Config
from .context import Ctx
from .events import EventLog
from .poster import PosterModule
from .settings import SCHEMA, SettingsStore


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every upstream request at INFO: one line per video chunk and
    # poster. Keep that out of the container log unless debugging.
    if level != "DEBUG":
        for noisy in ("httpx", "httpcore", "httpx2", "httpcore2"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


async def _admin_not_found(request: Request) -> JSONResponse:
    # Unknown /refused/... paths must never fall through to Remux.
    return JSONResponse({"error": "not found"}, status_code=404)


def create_app(
    config: Config | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """``config`` defaults to the environment; ``upstream_transport`` is a test hook."""
    config = config or Config.from_env()
    _setup_logging(config.log_level)
    config.data_dir.mkdir(parents=True, exist_ok=True)

    settings = SettingsStore(
        config.data_dir / "settings.json",
        {m: getattr(config, f"{m}_default", False) for m in SCHEMA},
    )
    ctx = Ctx(config, settings, EventLog())
    ctx.auth = Auth(config)
    ctx.poster = PosterModule(ctx)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        ctx.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
            transport=upstream_transport,
        )
        try:
            await ctx.poster.start()
            ctx.events.info("SYS", f"Refused {VERSION} up; upstream {config.remux_url}")
            yield
        finally:
            await ctx.poster.stop()
            await ctx.client.aclose()

    routes = admin.routes() + [
        Route(ADMIN_BASE, _admin_not_found, methods=proxy.METHODS),
        Route(ADMIN_BASE + "/{rest:path}", _admin_not_found, methods=proxy.METHODS),
        Route("/{path:path}", proxy.handle, methods=proxy.METHODS),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.ctx = ctx
    return app
