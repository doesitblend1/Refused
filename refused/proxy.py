"""The reverse proxy that sits between Infuse and Remux.

Everything is passed straight through (streamed, byte ranges intact) except
``200`` JSON bodies, which STREAM_STRIP / POSTER_REFRESH may rewrite.
Images are never buffered or modified.

With every module off this is a plain reverse proxy and even the client's
``Accept-Encoding`` is forwarded. Any failure while rewriting falls back to the
untouched upstream body.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, TypeVar

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .poster import match_image_path
from .settings import SEE_ALL_EVERYTHING
from .stripper import strip_json

log = logging.getLogger("refused.proxy")

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
# Request headers never forwarded as-is. Host is carried as X-Forwarded-Host
# instead, so Remux keeps advertising *this* address: it builds the LocalAddress
# that Jellyfin clients save from X-Forwarded-Host (then Host).
REQUEST_DROP = HOP_BY_HOP | {"host", "content-length", "expect"}
# Response headers the ASGI server sets itself; forwarding them duplicates them.
RESPONSE_DROP = HOP_BY_HOP | {"date", "server"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# JSON bodies above this are parsed / serialised in a worker thread so a big
# library listing does not stall video streams sharing the event loop.
OFFLOAD_BYTES = 256 * 1024

T = TypeVar("T")


async def _maybe_thread(big: bool, fn: Callable[..., T], *args) -> T:
    return await asyncio.to_thread(fn, *args) if big else fn(*args)


# ---- headers -------------------------------------------------------------------
def _upstream_request_headers(request: Request, identity: bool) -> list[tuple[str, str]]:
    """Headers for the upstream request. ``identity`` forces uncompressed bodies
    (needed whenever a module may read the body)."""
    headers = []
    xff = None
    client_encoding = False
    for raw_k, raw_v in request.headers.raw:
        k, v = raw_k.decode("latin-1"), raw_v.decode("latin-1")
        lk = k.lower()
        if lk in REQUEST_DROP:
            continue
        if lk == "accept-encoding":
            if identity:
                continue
            client_encoding = True
        if lk == "x-forwarded-for":
            xff = v if xff is None else f"{xff}, {v}"
            continue
        headers.append((k, v))
    if identity or not client_encoding:
        # Otherwise httpx adds its own "gzip, deflate" and the client could get
        # a compressed body it never asked for.
        headers.append(("accept-encoding", "identity"))
    client_ip = request.client.host if request.client else None
    if client_ip:
        xff = f"{xff}, {client_ip}" if xff else client_ip
    if xff:
        headers.append(("x-forwarded-for", xff))
    present = {k.lower() for k, _ in headers}
    host = request.headers.get("host")
    if host and "x-forwarded-host" not in present:  # keep one set by a proxy in front
        headers.append(("x-forwarded-host", host))
    if "x-forwarded-proto" not in present:
        headers.append(("x-forwarded-proto", request.url.scheme))
    return headers


def _relocate(location: str, base: str) -> str:
    """Rewrite an absolute redirect to Remux into a relative one."""
    if location == base or location.startswith(base + "/") or location.startswith(base + "?"):
        return location[len(base):] or "/"
    return location


def _response_headers(ctx, upstream: httpx.Response, *, keep_framing: bool) -> list[tuple[str, str]]:
    out = []
    for k, v in upstream.headers.multi_items():
        lk = k.lower()
        if lk in RESPONSE_DROP:
            continue
        if not keep_framing and lk in ("content-length", "content-encoding"):
            continue
        if lk == "location":
            v = _relocate(v, ctx.config.remux_url)
        out.append((lk, v))
    return out


def _with_headers(response: Response, headers: list[tuple[str, str]]) -> Response:
    """Copy headers onto a response, replacing its defaults but keeping
    repeated upstream headers (e.g. several Set-Cookie) intact."""
    seen: set[str] = set()
    for k, v in headers:
        if k in seen:
            response.headers.append(k, v)
        else:
            response.headers[k] = v
            seen.add(k)
    return response


# ---- body helpers ------------------------------------------------------------------
async def _read_capped(it: AsyncIterator[bytes], cap: int) -> tuple[bytes, AsyncIterator[bytes] | None]:
    """Read up to ``cap`` bytes. If the body is bigger, also return the iterator
    so the caller can stream the remainder on."""
    buf = bytearray()
    async for chunk in it:
        buf += chunk
        if len(buf) > cap:
            return bytes(buf), it
    return bytes(buf), None


async def _relay(upstream: httpx.Response, it: AsyncIterator[bytes], head: bytes = b""):
    try:
        if head:
            yield head
        async for chunk in it:
            yield chunk
    except httpx.HTTPError as exc:
        # Remux dropped mid-body. Ending here truncates the response, which the
        # client sees as a short read; better than an ASGI traceback per drop.
        log.warning("upstream body interrupted for %s: %s", upstream.request.url.path, exc)
    finally:
        await upstream.aclose()


def _streamed(ctx, upstream: httpx.Response, it: AsyncIterator[bytes], head: bytes = b"",
              *, keep_framing: bool) -> Response:
    return _with_headers(
        StreamingResponse(_relay(upstream, it, head), status_code=upstream.status_code),
        _response_headers(ctx, upstream, keep_framing=keep_framing),
    )


async def _transform_json(ctx, path: str, body: bytes, sink: dict | None = None) -> bytes | None:
    """Apply the enabled JSON modules. None means 'send the body unchanged'."""
    strip_cfg = ctx.settings.get("stream_strip")
    poster_cfg = ctx.settings.get("poster_refresh")
    is_playback = "playbackinfo" in path.lower()
    do_strip = strip_cfg["enabled"] and (strip_cfg["scope"] == "all" or is_playback)
    do_poster = poster_cfg["enabled"]
    if not (do_strip or do_poster):
        return None
    big = len(body) > OFFLOAD_BYTES
    try:
        data = await _maybe_thread(big, json.loads, body)
    except ValueError:
        return None
    changed = False
    try:
        if do_strip:
            names = ctx.strip_names(strip_cfg["fields"])
            streams, removed = await _maybe_thread(big, strip_json, data, names)
            if streams:
                changed = True
                s = ctx.strip_stats
                s["responses_stripped"] += 1
                s["streams_stripped"] += streams
                s["fields_removed"] += removed
                if is_playback:
                    ctx.events.info("STRIP", f"{path} streams={streams} fields_removed={removed}")
        # Poster rewriting touches shared state, so it stays on the event loop.
        if do_poster and ctx.poster.process_json(data, poster_cfg, sink):
            changed = True
        if not changed:
            return None
        return await _maybe_thread(
            big, lambda d: json.dumps(d, separators=(",", ":")).encode("utf-8"), data
        )
    except Exception:
        log.exception("rewrite failed for %s; passing response through unmodified", path)
        return None


# ---- SEE_ALL ---------------------------------------------------------------------------
def expand_limit(query: bytes, trigger: int, new: int) -> tuple[bytes, bool]:
    """Replace ``limit=<trigger>`` with ``limit=<new>`` in a raw query string,
    leaving every other byte as the client sent it."""
    wanted = str(trigger).encode()
    parts = query.split(b"&")
    changed = False
    for i, part in enumerate(parts):
        key, eq, value = part.partition(b"=")
        if eq and key.lower() == b"limit" and value.strip() == wanted:
            parts[i] = key + b"=" + str(new).encode()
            changed = True
    return b"&".join(parts), changed


def query_param(query: bytes, name: str) -> str | None:
    """First value of a raw query parameter, matched case-insensitively."""
    for part in query.split(b"&"):
        key, eq, value = part.partition(b"=")
        if eq and key.decode("latin-1").lower() == name:
            return value.decode("latin-1")
    return None


def plan_pages(n: int, page: int) -> tuple[int, list[tuple[int, int]]] | None:
    """Split ``n`` items into a first response and the follow-up pages Infuse
    will then ask for.

    Infuse treats the length of the first response as the list's total, asks for
    the remaining ``page``-sized pages as you scroll, and stacks every page it
    receives after the first one. So the first response must hold only part of
    the list and the follow-up pages the rest, in order. Returns ``(k, slices)``:
    the first response is items[:k], and follow-up page ``i`` is
    items[slices[i-1][0]:slices[i-1][1]]. Every page gets at least one item and
    never more than Infuse asks for; None when no clean split exists."""
    for k in range(page + 1, n):
        m = (k - 1) // page                      # follow-up pages Infuse will request
        limits = [page] * (m - 1) + [k - page * m]
        need = n - k
        if m <= need <= sum(limits):
            slices, pos, remaining = [], k, need
            for i, limit in enumerate(limits):
                take = min(limit, remaining - (m - i - 1))
                slices.append((pos, pos + take))
                pos += take
                remaining -= take
            return k, slices
    return None


def list_summary(rows: list) -> list[str]:
    """Plain-English lines describing a delivered list: how many different
    titles it holds, which titles repeat (same title under another Id), and
    its first and last entries. Pure diagnostics, never sent to Infuse."""
    def title(r: dict) -> str:
        name = str(r.get("Name") or "?")
        series = r.get("SeriesName")
        year = r.get("ProductionYear")
        text = f"{series} - {name}" if series else name
        return f"{text} ({year})" if year else text

    first_seen: dict[tuple, int] = {}
    repeats: list[tuple[int, int, str]] = []
    for pos, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        key = (r.get("Type"), r.get("SeriesName"), r.get("Name"), r.get("ProductionYear"))
        if key in first_seen:
            repeats.append((pos, first_seen[key], title(r)))
        else:
            first_seen[key] = pos
    lines = [f"list: {len(rows)} items, {len(first_seen)} different titles, "
             f"{len(repeats)} repeat an earlier title"]
    if repeats:
        lines.append("repeats: " + " | ".join(
            f"#{pos} {name} (also #{first})" for pos, first, name in repeats[:10]))
    ents = [(i, r) for i, r in enumerate(rows, 1) if isinstance(r, dict)]
    lines.append("first 8: " + " | ".join(f"#{i} {title(r)}" for i, r in ents[:8]))
    lines.append("last 12: " + " | ".join(f"#{i} {title(r)}" for i, r in ents[-12:]))
    return lines


def is_first_page(query: bytes) -> bool:
    """True when the request starts at the beginning of the list (no startIndex,
    or startIndex=0). Later pages must pass through untouched or Infuse would
    receive the same items again."""
    for part in query.split(b"&"):
        key, eq, value = part.partition(b"=")
        if eq and key.lower() == b"startindex":
            return value.strip() in (b"", b"0")
    return True


# ---- diagnostics -----------------------------------------------------------------------
_SECRET_PARAMS = frozenset({
    "api_key", "apikey", "x-emby-token", "x-mediabrowser-token", "token",
    "accesstoken", "access_token", "password", "pw", "x-emby-authorization",
})
_SHOWN_PARAMS = ("limit", "startindex", "parentid", "includeitemtypes", "sortby",
                 "sortorder", "filters", "isfavorite", "genres", "searchterm",
                 "recursive", "namestartswith")


def _safe_query(request: Request) -> dict[str, str]:
    """Query parameters worth showing, with secrets masked and values capped."""
    out: dict[str, str] = {}
    for k, v in request.query_params.multi_items():
        lk = k.lower()
        if lk in _SECRET_PARAMS:
            continue
        out[k] = v if len(v) <= 80 else v[:77] + "..."
    return out


def _diag_line(request: Request, status: int, data: bytes, scope: str) -> str | None:
    """One log line for this response, or None if it is not of interest."""
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    query = _safe_query(request)
    lower = {k.lower(): v for k, v in query.items()}
    items = doc.get("Items") if isinstance(doc, dict) else None
    is_list = isinstance(items, list) or isinstance(doc, list) \
        or "limit" in lower or "startindex" in lower
    if scope == "lists" and not is_list:
        return None
    parts = [f"{request.method} {request.url.path}"]
    shown = [f"{k}={v}" for k, v in query.items() if k.lower() in _SHOWN_PARAMS]
    if shown:
        parts.append(" ".join(shown))
    hidden = len(query) - len(shown)
    if hidden > 0:
        parts.append(f"(+{hidden} other params)")
    parts.append(f"-> {status}")
    if isinstance(items, list):
        total = doc.get("TotalRecordCount")
        parts.append(f"items={len(items)}" + (f" total={total}" if total is not None else ""))
    elif isinstance(doc, list):
        parts.append(f"items={len(doc)}")
    return " ".join(parts)


# ---- handler ---------------------------------------------------------------------------
async def handle(request: Request) -> Response:
    ctx = request.app.state.ctx
    ctx.proxy_stats["requests"] += 1
    method = request.method
    path = request.url.path
    strip_cfg = ctx.settings.get("stream_strip")
    poster_cfg = ctx.settings.get("poster_refresh")
    diag_cfg = ctx.settings.get("diagnostics")
    active = strip_cfg["enabled"] or poster_cfg["enabled"]
    # Diagnostics only reads bodies; it never rewrites them.
    see_cfg = ctx.settings.get("see_all")
    reads_json = active or diag_cfg["enabled"]

    # SEARCH_CLEAN: Infuse asks for people in a request of its own, so it can be
    # answered here without troubling Remux.
    search_cfg = ctx.settings.get("search_clean")
    if (search_cfg["enabled"] and "People" in search_cfg["hide"] and method == "GET"
            and path.lower().endswith("/persons")):
        term = query_param(request.scope.get("query_string", b""), "searchterm")
        if term:
            ctx.search_stats["people_searches_hidden"] += 1
            ctx.events.info("SEARCH_CLEAN", f"{path}: answered empty for a search (cast and crew hidden)")
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

    raw_path = request.scope.get("raw_path") or path.encode()
    query = request.scope.get("query_string", b"")
    expanded = None
    is_latest = (see_cfg["enabled"] and method == "GET" and query
                 and path.lower().endswith("/items/latest"))
    catalog = (query_param(query, "parentid") or "").lower() if is_latest else ""
    if is_latest and not is_first_page(query):
        held = ctx.see_all_served.get(catalog)
        if held is not None:
            page_size, pages = held
            try:
                start = int(query_param(query, "startindex") or 0)
            except ValueError:
                start = -1
            slot = start // page_size if start >= 0 and start % page_size == 0 else 0
            if 1 <= slot <= len(pages):
                # The rest of the list Infuse was promised in the first response.
                ctx.see_all_stats["pages_served"] += 1
                ctx.events.info("SEE_ALL", f"{path} startIndex={start}: sent page {slot} of "
                                f"{len(pages)} ({len(pages[slot - 1])} items)")
                pending = ctx.see_all_pending.get(catalog)
                if pending:
                    ctx.poster.release(pages[slot - 1], pending)
                return JSONResponse(pages[slot - 1])
    if is_latest and is_first_page(query):
        new_limit = see_cfg["max_items"] or SEE_ALL_EVERYTHING
        if new_limit > see_cfg["trigger_limit"]:
            query, did = expand_limit(query, see_cfg["trigger_limit"], new_limit)
            if did:
                expanded = new_limit
                reads_json = True  # so the result can be counted and logged
    target = ctx.config.remux_url + raw_path.decode("latin-1")
    if query:
        target += "?" + query.decode("latin-1")

    body = await request.body()
    try:
        upstream = await ctx.client.send(
            ctx.client.build_request(
                method, target,
                headers=_upstream_request_headers(request, identity=reads_json),
                content=body or None,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        ctx.proxy_stats["upstream_errors"] += 1
        ctx.events.warn("PROXY", f"upstream failed: {method} {path}: {type(exc).__name__}")
        return JSONResponse({"error": "upstream request failed"}, status_code=502)

    status = upstream.status_code
    ctype = upstream.headers.get("content-type", "").lower()

    # ---- bodyless --------------------------------------------------------------------
    if method == "HEAD" or status in (204, 304) or status < 200:
        await upstream.aclose()
        return _with_headers(
            Response(status_code=status), _response_headers(ctx, upstream, keep_framing=True)
        )

    # ---- image requests: logged on request, never modified --------------------------------------------------------------------------
    image = match_image_path(path) if method == "GET" and poster_cfg["enabled"] else None
    if image and poster_cfg["log_image_requests"]:
        ctx.events.info(
            "IMAGE",
            f"{image[1]} {image[0][:8]} tag={request.query_params.get('tag', '-')[:12]} -> {status}",
        )
    # ---- JSON rewriting --------------------------------------------------------------------
    if reads_json and status == 200 and "json" in ctype and method in ("GET", "POST"):
        data, rest = await _read_capped(upstream.aiter_bytes(), ctx.config.max_json_mb * 1024 * 1024)
        if rest is not None:
            return _streamed(ctx, upstream, rest, data, keep_framing=False)
        await upstream.aclose()
        sink: dict | None = {} if expanded else None   # park checks until delivery
        rewritten = await _transform_json(ctx, path, data, sink) if active else None
        final = data if rewritten is None else rewritten
        if expanded:
            ctx.see_all_stats["requests_expanded"] += 1
            n = unique = None
            rows = []
            try:
                doc = json.loads(final)
                rows = doc if isinstance(doc, list) else doc.get("Items", [])
                n = len(rows)
                unique = len({r.get("Id") for r in rows if isinstance(r, dict)})
            except (ValueError, AttributeError):
                pass
            if n is not None:
                ctx.see_all_stats["last_items_returned"] = n
            split = None
            if n and n > see_cfg["trigger_limit"]:
                split = plan_pages(n, see_cfg["trigger_limit"])
            if split:
                k, slices = split
                final = json.dumps(rows[:k], separators=(",", ":")).encode("utf-8")
                ctx.see_all_served.pop(catalog, None)       # re-insert as newest
                ctx.see_all_pending.pop(catalog, None)
                while len(ctx.see_all_served) >= 20:
                    oldest = next(iter(ctx.see_all_served))
                    del ctx.see_all_served[oldest]
                    ctx.see_all_pending.pop(oldest, None)
                ctx.see_all_served[catalog] = (
                    see_cfg["trigger_limit"], [rows[a:b] for a, b in slices])
                # Poster checks for the held-back items wait until Infuse asks for them.
                ctx.see_all_pending[catalog] = sink or {}
                if sink:
                    ctx.poster.release(rows[:k], sink)
            else:
                ctx.see_all_served.pop(catalog, None)
                ctx.see_all_pending.pop(catalog, None)
                if sink:
                    ctx.poster.release(rows, sink)
            ctx.events.info(
                "SEE_ALL",
                f"{path} limit {see_cfg['trigger_limit']} -> {expanded}: Remux returned "
                + (f"{n} items ({unique} unique)" if n is not None else "an unreadable body"),
            )
            if split:
                ctx.events.info("SEE_ALL", f"sent the first {split[0]} now; the other {n - split[0]} "
                                f"follow in {len(split[1])} pages as Infuse asks for them")
            if n:
                try:
                    for line in list_summary(rows):
                        ctx.events.info("SEE_ALL", line)
                except Exception:  # never let a diagnostic break the response
                    log.exception("list summary failed")
        if diag_cfg["enabled"]:
            try:
                line = _diag_line(request, status, data, diag_cfg["scope"])
            except Exception:  # diagnostics must never break a response
                log.exception("diagnostics failed for %s", path)
                line = None
            if line:
                ctx.diag_stats["requests_logged"] += 1
                ctx.events.info("REQUEST", line)
        return _with_headers(
            Response(content=final, status_code=status),
            _response_headers(ctx, upstream, keep_framing=False),
        )

    # ---- everything else: stream straight through ------------------------------------------
    return _streamed(ctx, upstream, upstream.aiter_raw(), keep_framing=True)
