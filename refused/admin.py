"""The /refused admin panel: static UI + a small JSON API behind a login."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .auth import COOKIE
from .config import ADMIN_BASE, VERSION
from .poster import CANONICAL_TYPES
from .settings import SCHEMA, ValidationError, public_schema

UI_DIR = Path(__file__).parent / "ui"
ID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
MAX_BODY = 64 * 1024

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'none'"
    ),
}

_STATIC_TYPES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
_static_cache: dict[str, bytes] = {}


def _json(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def _err(message: str, status: int, **extra) -> JSONResponse:
    return _json({"error": message, **extra}, status)


async def _body(request: Request) -> dict | None:
    """Parse a small JSON object body; None if oversized or malformed."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            return None
    raw = bytes(buf)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def authed(handler):
    """Require a valid session; require the CSRF header on anything but GET."""

    async def wrapper(request: Request) -> Response:
        ctx = request.app.state.ctx
        session = ctx.auth.verify(request)
        if session is None:
            return _err("not signed in", 401)
        if request.method not in ("GET", "HEAD"):
            sent = request.headers.get("x-csrf-token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                return _err("bad CSRF token", 403)
        return await handler(request, session)

    return wrapper


# ---- pages -----------------------------------------------------------------
_index_html: str | None = None


async def index(request: Request) -> Response:
    global _index_html
    if _index_html is None:
        _index_html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(_index_html, headers=SECURITY_HEADERS)


async def static(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in _STATIC_TYPES:
        return Response("not found", status_code=404)
    if name not in _static_cache:
        _static_cache[name] = (UI_DIR / name).read_bytes()
    data = _static_cache[name]
    etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
    headers = {**SECURITY_HEADERS, "Cache-Control": "no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=_STATIC_TYPES[name], headers=headers)


async def healthz(request: Request) -> Response:
    ctx = request.app.state.ctx
    return _json(
        {
            "ok": True,
            "version": VERSION,
            "uptime_seconds": int(time.time() - ctx.started_at),
            "modules": {m: ctx.settings.get(m)["enabled"] for m in SCHEMA},
        }
    )


# ---- auth ----------------------------------------------------------------------
async def session_info(request: Request) -> Response:
    ctx = request.app.state.ctx
    session = ctx.auth.verify(request)
    if session is None:
        return _json({"authenticated": False, "version": VERSION})
    return _json(
        {
            "authenticated": True,
            "user": session["u"],
            "csrf": session["csrf"],
            "version": VERSION,
        }
    )


async def login(request: Request) -> Response:
    ctx = request.app.state.ctx
    ip = _client_ip(request)
    wait = ctx.auth.retry_after(ip)
    if wait:
        resp = _err(f"too many failed attempts, try again in {wait // 60 + 1} min", 429)
        resp.headers["Retry-After"] = str(wait)
        return resp
    data = await _body(request)
    if data is None:
        return _err("bad request", 400)
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return _err("bad request", 400)
    if not ctx.auth.check_credentials(username, password):
        ctx.auth.record_failure(ip)
        ctx.events.warn("AUTH", f"failed sign-in from {ip}")
        await asyncio.sleep(0.6)
        return _err("ACCESS DENIED", 401)
    ctx.auth.record_success(ip)
    cookie, csrf = ctx.auth.issue()
    resp = _json({"ok": True, "user": username, "csrf": csrf})
    resp.set_cookie(COOKIE, cookie, **ctx.auth.cookie_kwargs())
    ctx.events.info("AUTH", f"signed in from {ip}")
    return resp


async def logout(request: Request) -> Response:
    ctx = request.app.state.ctx
    session = ctx.auth.verify(request)
    if session is not None:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, session.get("csrf", "")):
            return _err("bad CSRF token", 403)
    resp = _json({"ok": True})
    resp.delete_cookie(COOKIE, path=ADMIN_BASE)
    return resp


# ---- state / settings -------------------------------------------------------------
async def _upstream(ctx) -> dict:
    now = time.time()
    if ctx.upstream_cache and now - ctx.upstream_cache[0] < 15:
        return ctx.upstream_cache[1]
    try:
        r = await ctx.client.get(
            ctx.config.remux_url + "/System/Info/Public", timeout=httpx.Timeout(3.0)
        )
        result = {"reachable": True, "http_status": r.status_code}
        try:
            info = r.json()
            if isinstance(info, dict):
                for key, name in (("Version", "version"), ("ServerName", "server_name")):
                    if isinstance(info.get(key), str) and info[key]:
                        result[name] = info[key][:64]
        except ValueError:
            pass
    except httpx.HTTPError as exc:
        result = {"reachable": False, "error": type(exc).__name__}
    ctx.upstream_cache = (now, result)
    return result


def _module_stats(ctx, module: str) -> dict:
    if module == "stream_strip":
        return dict(ctx.strip_stats)
    if module == "diagnostics":
        return dict(ctx.diag_stats)
    if module == "see_all":
        return dict(ctx.see_all_stats)
    if module == "search_clean":
        return dict(ctx.search_stats)
    return ctx.poster.public_stats()


@authed
async def state(request: Request, session: dict) -> Response:
    ctx = request.app.state.ctx
    modules = {}
    for m in SCHEMA:
        modules[m] = {"settings": ctx.settings.get(m), "stats": _module_stats(ctx, m)}
    return _json(
        {
            "version": VERSION,
            "uptime_seconds": int(time.time() - ctx.started_at),
            "upstream": {"url": ctx.config.remux_url, **(await _upstream(ctx))},
            "proxy": dict(ctx.proxy_stats),
            "schema": public_schema(),
            "modules": modules,
        }
    )


@authed
async def put_settings(request: Request, session: dict) -> Response:
    ctx = request.app.state.ctx
    module = request.path_params["module"]
    if module not in SCHEMA:
        return _err("unknown module", 404)
    data = await _body(request)
    if data is None:
        return _err("bad request", 400)
    try:
        if data.get("reset") is True:
            new = ctx.settings.reset(module)
            ctx.events.info("ADMIN", f"{module}: settings reset to defaults")
        else:
            before = ctx.settings.get(module)
            new = ctx.settings.update(module, data)
            changed = sorted(k for k in new if new[k] != before.get(k))
            if "enabled" in changed:
                ctx.events.info(
                    "ADMIN", f"{module}: {'ENABLED' if new['enabled'] else 'DISABLED'}"
                )
                changed.remove("enabled")
            if changed:
                ctx.events.info("ADMIN", f"{module}: updated {', '.join(changed)}")
    except ValidationError as exc:
        return _err("invalid settings", 422, errors=exc.errors)
    return _json({"settings": new})


# ---- poster actions ------------------------------------------------------------------
@authed
async def poster_force_refresh(request: Request, session: dict) -> Response:
    ctx = request.app.state.ctx
    epoch = await ctx.poster.force_refresh()
    return _json({"ok": True, "epoch": epoch})


@authed
async def poster_purge(request: Request, session: dict) -> Response:
    ctx = request.app.state.ctx
    n = await ctx.poster.purge()
    return _json({"ok": True, "forgotten": n})


@authed
async def poster_probe(request: Request, session: dict) -> Response:
    ctx = request.app.state.ctx
    data = await _body(request)
    if data is None:
        return _err("bad request", 400)
    item_id = str(data.get("item_id", "")).strip()
    img_type = CANONICAL_TYPES.get(str(data.get("image_type", "Primary")).lower())
    if not ID_RE.match(item_id):
        return _err("item id must be a Remux item id (hex/UUID)", 422)
    if not img_type:
        return _err("unknown image type", 422)
    result = await ctx.poster.probe_now(item_id, img_type)
    return _json({"probe": result, "records": ctx.poster.inspect(item_id)})


# ---- log ----------------------------------------------------------------------------------
@authed
async def events(request: Request, session: dict) -> Response:
    ctx = request.app.state.ctx
    try:
        after = int(request.query_params.get("after", "0"))
    except ValueError:
        after = 0
    return _json({"events": ctx.events.since(after), "last_id": ctx.events.last_id})


def routes() -> list[Route]:
    b = ADMIN_BASE
    return [
        Route(b, index, methods=["GET"]),
        Route(b + "/", index, methods=["GET"]),
        Route(b + "/healthz", healthz, methods=["GET"]),
        Route(b + "/static/{name}", static, methods=["GET"]),
        Route(b + "/api/session", session_info, methods=["GET"]),
        Route(b + "/api/login", login, methods=["POST"]),
        Route(b + "/api/logout", logout, methods=["POST"]),
        Route(b + "/api/state", state, methods=["GET"]),
        Route(b + "/api/settings/{module}", put_settings, methods=["PUT"]),
        Route(b + "/api/poster/force-refresh", poster_force_refresh, methods=["POST"]),
        Route(b + "/api/poster/purge", poster_purge, methods=["POST"]),
        Route(b + "/api/poster/probe", poster_probe, methods=["POST"]),
        Route(b + "/api/events", events, methods=["GET"]),
    ]
