"""A tiny stand-in for Remux's Jellyfin-style API, just enough to test Refused."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

ID1 = "11111111-1111-1111-1111-111111111111"
ID2 = "22222222-2222-2222-2222-222222222222"
ID3 = "33333333-3333-3333-3333-333333333333"
VIDEO = bytes((i * 7) % 251 for i in range(3_000_000))


def make_jpeg(seed: int, size=(400, 600)) -> bytes:
    """Deterministic stand-in image bytes (a JPEG header plus seed-derived
    payload). Refused never decodes images, so real pixels are not needed."""
    w, h = size
    body = bytes((seed * 31 + i * 7) % 256 for i in range(w * 3))
    return b"\xff\xd8\xff\xe0" + f"{seed}:{w}x{h}:".encode() + body + b"\xff\xd9"


def streams():
    return [
        {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "BitRate": 40000000,
         "BitDepth": 10, "VideoRangeType": "DOVI", "DisplayTitle": "4K HEVC", "Language": "eng",
         "Index": 0, "IsDefault": True},
        {"Type": "Audio", "Codec": "truehd", "Channels": 8, "ChannelLayout": "7.1",
         "SampleRate": 48000, "DisplayTitle": "English TrueHD", "Language": "eng", "Index": 1},
    ]


@dataclass
class MockRemux:
    images: dict = field(default_factory=dict)       # (id, type) -> bytes
    tags: dict = field(default_factory=dict)         # (id, type) -> tag
    hits: dict = field(default_factory=dict)         # (id, type) -> count
    seen_headers: list = field(default_factory=list)
    seen_queries: list = field(default_factory=list)
    latest_total: int = 120                          # size of the mock "latest" catalog

    def __post_init__(self):
        for n, iid in enumerate((ID1, ID2, ID3), start=1):
            self.images[(iid, "Primary")] = make_jpeg(n)
            self.tags[(iid, "Primary")] = f"{n}0000000-aaaa-bbbb-cccc-{n:012d}"
        self.images[(ID1, "Backdrop")] = make_jpeg(9, (640, 360))
        self.tags[(ID1, "Backdrop")] = "bd000000-aaaa-bbbb-cccc-000000000001"
        self.app = self._build()

    # ---- helpers for tests ---------------------------------------------
    def swap_image(self, iid: str, typ: str = "Primary", seed: int = 99, **kw):
        self.images[(iid, typ)] = make_jpeg(seed, **kw)

    def item(self, iid: str, typ: str, name: str) -> dict:
        d = {
            "Id": iid, "Type": typ, "Name": name,
            "ImageTags": {"Primary": self.tags[(iid, "Primary")]},
            "BackdropImageTags": [],
            "MediaSources": [{"Id": iid, "Name": name, "MediaStreams": streams()}],
        }
        if (iid, "Backdrop") in self.tags:
            d["ImageTags"]["Backdrop"] = self.tags[(iid, "Backdrop")]
            d["BackdropImageTags"] = [self.tags[(iid, "Backdrop")]]
        if typ == "Episode":
            # Remux repeats parent artwork tags on children (see models.rs).
            d["SeriesId"] = ID2
            d["SeriesPrimaryImageTag"] = self.tags[(ID2, "Primary")]
            d["ParentBackdropItemId"] = ID1
            d["ParentBackdropImageTags"] = [self.tags[(ID1, "Backdrop")]]
            d["ParentThumbItemId"] = ID1
            d["ParentThumbImageTag"] = self.tags[(ID1, "Backdrop")]  # thumb falls back to backdrop
        return d

    # ---- routes ----------------------------------------------------------
    def _build(self) -> Starlette:
        async def items(request: Request):
            self.seen_headers.append(dict(request.headers))
            self.seen_queries.append(request.url.query)
            return JSONResponse({
                "Items": [self.item(ID1, "Movie", "Alpha"), self.item(ID2, "Series", "Beta"),
                          self.item(ID3, "Episode", "Gamma")],
                "TotalRecordCount": 3,
            }, headers={"X-Remux-Test": "yes"})

        async def latest(request: Request):
            self.seen_queries.append(request.url.query)
            limit = int(request.query_params.get("limit", request.query_params.get("Limit", "20")))
            n = min(limit, self.latest_total)
            return JSONResponse([{"Id": f"{i:032x}", "Name": f"Latest {i}", "Type": "Movie",
                                  "ImageTags": {"Primary": f"tag{i}"}} for i in range(n)])

        async def persons(request: Request):
            self.seen_queries.append("PERSONS " + request.url.query)
            return JSONResponse({"Items": [{"Id": "p1", "Name": "Will Smith", "Type": "Person"}],
                                 "TotalRecordCount": 1, "StartIndex": 0})

        async def playback(request: Request):
            return JSONResponse({"MediaSources": [{"Id": "a", "Name": "Version A", "MediaStreams": streams()}],
                                 "PlaySessionId": "s"})

        async def image(request: Request):
            iid, typ = request.path_params["iid"], request.path_params["typ"].capitalize()
            body = self.images.get((iid, typ))
            if body is None:
                return Response("nope", status_code=404)
            self.hits[(iid, typ)] = self.hits.get((iid, typ), 0) + 1
            return Response(body, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

        async def stream(request: Request):
            rng = request.headers.get("range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                a = int(a); b = int(b) if b else len(VIDEO) - 1
                part = VIDEO[a:b + 1]
                return Response(part, status_code=206, media_type="video/mp4", headers={
                    "Content-Range": f"bytes {a}-{b}/{len(VIDEO)}", "Accept-Ranges": "bytes"})
            return Response(VIDEO, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})

        async def redirect(request: Request):
            return Response(status_code=302, headers={"Location": "http://remux.test/Items?x=1"})

        async def fav(request: Request):
            return Response(status_code=204)

        async def info(request: Request):
            return JSONResponse({"ServerName": "mock", "Version": "0"})

        async def echo(request: Request):
            body = await request.body()
            return JSONResponse({"method": request.method, "body": body.decode(),
                                 "cookie": request.headers.get("cookie"),
                                 "ae": request.headers.get("accept-encoding"),
                                 "xff": request.headers.get("x-forwarded-for"),
                                 "xfh": request.headers.get("x-forwarded-host"),
                                 "xfp": request.headers.get("x-forwarded-proto"),
                                 "host": request.headers.get("host"),
                                 "expect": request.headers.get("expect")})

        async def text(request: Request):
            return Response("plain text", media_type="text/plain",
                            headers={"Date": "Mon, 01 Jan 2001 00:00:00 GMT", "Server": "remux"})

        async def cookies(request: Request):
            r = Response("ok")
            r.headers.append("set-cookie", "a=1; Path=/")
            r.headers.append("set-cookie", "b=2; Path=/")
            return r

        return Starlette(routes=[
            Route("/Items", items),
            Route("/Items/Latest", latest),
            Route("/Persons", persons),
            Route("/Users/{u}/Items/Latest", latest),
            Route("/Items/{iid}/PlaybackInfo", playback, methods=["GET", "POST"]),
            Route("/Items/{iid}/Images/{typ}", image),
            Route("/Items/{iid}/Images/{typ}/{idx:int}", image),
            Route("/Videos/{iid}/stream", stream),
            Route("/redir", redirect),
            Route("/Users/{u}/FavoriteItems/{i}", fav, methods=["DELETE", "POST"]),
            Route("/System/Info/Public", info),
            Route("/echo", echo, methods=["GET", "POST", "PUT"]),
            Route("/text", text),
            Route("/cookies", cookies),
        ])
