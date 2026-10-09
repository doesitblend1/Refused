from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from refused.config import Config, ConfigError
from refused.main import create_app
from refused.poster import make_tag, match_image_path
from refused.settings import SCHEMA, SettingsStore, ValidationError, coerce

from .mock_remux import ID1, ID2, ID3, VIDEO, MockRemux, make_jpeg

USER, PASSWORD = "admin", "s3cret-pass"


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mock = MockRemux()
        self._up = False
        await self.boot()
        self.addAsyncCleanup(self.shutdown)

    async def boot(self, transport=None):
        cfg = Config(remux_url="http://remux.test", username=USER, password=PASSWORD,
                     data_dir=Path(self.tmp.name))
        self.app = create_app(cfg, upstream_transport=transport or httpx.ASGITransport(app=self.mock.app))
        self._cm = self.app.router.lifespan_context(self.app)
        await self._cm.__aenter__()
        self.ctx = self.app.state.ctx
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url="http://refused.test")
        self._up = True

    async def shutdown(self):
        if self._up:
            self._up = False
            await self.http.aclose()
            await self._cm.__aexit__(None, None, None)

    # ---- helpers ------------------------------------------------------------
    def enable(self, module, **kw):
        self.ctx.settings.update(module, {"enabled": True, **kw})

    async def wait_for(self, pred, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            await asyncio.sleep(0.02)
        self.fail("condition not met in time")

    async def items(self):
        r = await self.http.get("/Items")
        self.assertEqual(r.status_code, 200)
        return r.json()["Items"]

    @staticmethod
    def primary(items, iid):
        return next(i for i in items if i["Id"] == iid)["ImageTags"]["Primary"]

    def poster_fast(self, **kw):
        self.enable("poster_refresh", probe_concurrency=4, **kw)

    async def recheck(self, expect_checks=2):
        """Force every tracked image to be due, browse, and wait for the checks."""
        before = self.ctx.poster.stats["checks_ok"] + self.ctx.poster.stats["checks_failed"]
        for rec in self.ctx.poster.records.values():
            rec.next_check = 0
        await self.items()
        await self.wait_for(
            lambda: self.ctx.poster.stats["checks_ok"] + self.ctx.poster.stats["checks_failed"]
            >= before + expect_checks
        )
        await asyncio.sleep(0.05)

    async def baseline(self):
        await self.items()
        await self.wait_for(lambda: self.ctx.poster.stats["checks_ok"] >= 2)
        await asyncio.sleep(0.05)

    async def login(self):
        r = await self.http.post("/refused/api/login", json={"username": USER, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["csrf"]


# ===========================================================================
class ConfigTests(unittest.TestCase):
    def test_requires_credentials(self):
        with self.assertRaises(ConfigError) as cm:
            Config(username="", password="").validate()
        self.assertIn("REFUSED_USERNAME", str(cm.exception))
        self.assertIn("REFUSED_PASSWORD", str(cm.exception))

    def test_rejects_placeholder_password(self):
        with self.assertRaises(ConfigError):
            Config(username="a", password="change_me").validate()

    def test_rejects_bad_url(self):
        with self.assertRaises(ConfigError):
            Config(username="a", password="b", remux_url="remux:3000").validate()


class SettingsTests(unittest.TestCase):
    def test_coerce_rejects_bad_values(self):
        fields = {f["key"]: f for f in SCHEMA["poster_refresh"]["fields"]}
        with self.assertRaises(ValueError):
            coerce(fields["recheck_hours"], 0)
        with self.assertRaises(ValueError):
            coerce(fields["recheck_hours"], "24")
        with self.assertRaises(ValueError):
            coerce(fields["recheck_hours"], True)
        with self.assertRaises(ValueError):
            coerce(fields["image_types"], "Primary")
        with self.assertRaises(ValueError):
            coerce(fields["image_types"], ["Primary", "Banner"])
        self.assertEqual(coerce(fields["image_types"], ["Thumb", "Primary", "Primary"]), ["Primary", "Thumb"])
        self.assertEqual(coerce(fields["recheck_hours"], 12.0), 12)

    def test_lines_normalised(self):
        f = next(x for x in SCHEMA["stream_strip"]["fields"] if x["key"] == "fields")
        self.assertEqual(coerce(f, [" Width ", "", "width", "Height"]), ["Width", "Height"])
        with self.assertRaises(ValueError):
            coerce(f, ['bad"name'])

    def test_update_persist_reload_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            s = SettingsStore(path)
            self.assertFalse(s.get("poster_refresh")["enabled"])
            s.update("poster_refresh", {"enabled": True, "recheck_hours": 6})
            s2 = SettingsStore(path)
            self.assertTrue(s2.get("poster_refresh")["enabled"])
            self.assertEqual(s2.get("poster_refresh")["recheck_hours"], 6)
            with self.assertRaises(ValidationError) as cm:
                s2.update("poster_refresh", {"recheck_hours": 99999, "nope": 1})
            self.assertEqual(len(cm.exception.errors), 2)
            self.assertEqual(s2.get("poster_refresh")["recheck_hours"], 6)  # untouched
            s2.reset("poster_refresh")
            self.assertEqual(s2.get("poster_refresh")["recheck_hours"], 24)
            self.assertTrue(s2.get("poster_refresh")["enabled"])  # reset keeps the switch

    def test_corrupt_file_and_bad_entries_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text("{not json")
            self.assertEqual(SettingsStore(path).get("stream_strip")["scope"], "all")
            path.write_text(json.dumps({"stream_strip": {"enabled": True, "scope": "bogus"},
                                        "ghost": {"enabled": True}}))
            s = SettingsStore(path)
            self.assertTrue(s.get("stream_strip")["enabled"])
            self.assertEqual(s.get("stream_strip")["scope"], "all")

    def test_settings_from_older_version_with_removed_keys_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({"poster_refresh": {
                "enabled": True, "recheck_hours": 7,
                "downscale_enabled": True, "downscale_poster_height": 900,
                "cache_control": "long"}}))
            s = SettingsStore(path)
            self.assertTrue(s.get("poster_refresh")["enabled"])
            self.assertEqual(s.get("poster_refresh")["recheck_hours"], 7)
            self.assertNotIn("downscale_enabled", s.get("poster_refresh"))
            s.update("poster_refresh", {"recheck_hours": 8})
            self.assertNotIn("downscale", path.read_text())    # dropped on next save
            self.assertNotIn("cache_control", path.read_text())

    def test_enabled_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            s = SettingsStore(Path(d) / "s.json", {"stream_strip": True})
            self.assertTrue(s.get("stream_strip")["enabled"])
            self.assertFalse(s.get("poster_refresh")["enabled"])

    def test_helpers(self):
        self.assertEqual(match_image_path("/Items/" + ID1 + "/Images/Primary"), (ID1, "Primary"))
        self.assertEqual(match_image_path("/items/" + ID1 + "/images/backdrop/0"), (ID1, "Backdrop"))
        self.assertIsNone(match_image_path("/Items/" + ID1 + "/Images/Banner"))
        self.assertIsNone(match_image_path("/Items/x/Images/Primary"))
        self.assertEqual(make_tag("orig", 0, 0), "orig")
        t = make_tag("orig", 1, 0)
        self.assertEqual(len(t), 32)
        self.assertNotIn("://", t)
        self.assertEqual(t, make_tag("orig", 1, 0))
        self.assertNotEqual(t, make_tag("orig", 2, 0))
        self.assertNotEqual(make_tag("orig", 0, 1), "orig")


# ===========================================================================
class ProxyTests(Base):
    async def test_modules_off_is_transparent(self):
        r = await self.http.get("/Items?Fields=a&Fields=b&q=%C3%A9", headers={"X-Emby-Token": "tok", "Cookie": "c=1"})
        direct = await httpx.AsyncClient(transport=httpx.ASGITransport(app=self.mock.app),
                                         base_url="http://remux.test").get("/Items")
        self.assertEqual(r.json(), direct.json())
        self.assertEqual(r.headers["x-remux-test"], "yes")
        self.assertIn("Fields=a&Fields=b&q=%C3%A9", self.mock.seen_queries[0])
        self.assertEqual(self.mock.seen_headers[0]["x-emby-token"], "tok")
        # streams untouched
        self.assertIn("Codec", r.json()["Items"][0]["MediaSources"][0]["MediaStreams"][0])

    async def test_post_body_and_headers_forwarded(self):
        r = await self.http.post("/echo", content=b'{"a":1}', headers={"Cookie": "k=v", "Accept-Encoding": "gzip"})
        j = r.json()
        self.assertEqual(j["body"], '{"a":1}')
        self.assertEqual(j["cookie"], "k=v")
        self.assertEqual(j["ae"], "gzip")          # modules off: client's encoding forwarded
        self.assertTrue(j["xff"])
        req = self.http.build_request("POST", "/echo", content=b"{}")
        del req.headers["accept-encoding"]
        j = (await self.http.send(req)).json()
        self.assertEqual(j["ae"], "identity")      # client asked for none: never invent one
        self.enable("stream_strip")
        j = (await self.http.post("/echo", content=b"{}", headers={"Accept-Encoding": "gzip"})).json()
        self.assertEqual(j["ae"], "identity")      # a module may rewrite: ask for plain bytes

    async def test_forwarded_host_keeps_remux_advertising_the_proxy(self):
        # Remux builds System/Info LocalAddress from X-Forwarded-Host, then Host.
        # Jellyfin clients save it, so it must name Refused, not Remux.
        j = (await self.http.get("/echo", headers={"Host": "192.168.1.50:8099", "Expect": "100-continue"})).json()
        self.assertEqual(j["xfh"], "192.168.1.50:8099")
        self.assertEqual(j["xfp"], "http")
        self.assertEqual(j["host"], "remux.test")
        self.assertIsNone(j["expect"])
        j = (await self.http.get("/echo", headers={"X-Forwarded-Host": "media.example", "X-Forwarded-Proto": "https"})).json()
        self.assertEqual((j["xfh"], j["xfp"]), ("media.example", "https"))  # front proxy wins

    async def test_xff_is_appended_not_replaced(self):
        j = (await self.http.get("/echo", headers={"X-Forwarded-For": "10.0.0.9"})).json()
        self.assertTrue(j["xff"].startswith("10.0.0.9, "))

    async def test_server_and_date_headers_not_duplicated(self):
        r = await self.http.get("/text")
        self.assertNotIn("date", r.headers)    # the ASGI server adds its own
        self.assertNotIn("server", r.headers)

    async def test_relocate_respects_host_boundary(self):
        from refused.proxy import _relocate
        self.assertEqual(_relocate("http://remux.test/Items?x=1", "http://remux.test"), "/Items?x=1")
        self.assertEqual(_relocate("http://remux.test", "http://remux.test"), "/")
        self.assertEqual(_relocate("http://remux.test:30001/x", "http://remux.test:3000"), "http://remux.test:30001/x")

    async def test_upstream_dropping_mid_body_is_contained(self):
        class Flaky(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"first-chunk"
                raise httpx.ReadError("connection reset")

        class T(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, headers={"content-type": "video/mp4"}, stream=Flaky())

        await self.shutdown()
        await self.boot(T())
        r = await self.http.get("/Videos/x/stream")
        self.assertEqual(r.content, b"first-chunk")

    async def test_unknown_admin_paths_never_reach_remux(self):
        for method, path in [("GET", "/refused/nope"), ("POST", "/refused"), ("GET", "/refused/api/zzz"),
                             ("GET", "/refused/api/login")]:
            r = await self.http.request(method, path)
            self.assertEqual(r.status_code, 404, (method, path))
            self.assertEqual(r.json(), {"error": "not found"})   # answered by Refused, not Remux

    async def test_large_json_is_processed_off_loop(self):
        import refused.proxy as px
        old = px.OFFLOAD_BYTES
        px.OFFLOAD_BYTES = 0
        try:
            self.enable("stream_strip")
            self.enable("poster_refresh")
            v = (await self.items())[0]["MediaSources"][0]["MediaStreams"][0]
            self.assertNotIn("Codec", v)
        finally:
            px.OFFLOAD_BYTES = old

    async def test_httpx_request_logging_is_quiet(self):
        import logging
        self.assertGreaterEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)

    async def test_range_requests_stream_through_exactly(self):
        full = await self.http.get("/Videos/x/stream")
        self.assertEqual(full.content, VIDEO)
        part = await self.http.get("/Videos/x/stream", headers={"Range": "bytes=100-199"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, VIDEO[100:200])
        self.assertEqual(part.headers["content-range"], f"bytes 100-199/{len(VIDEO)}")

    async def test_misc_statuses_pass_through(self):
        self.enable("stream_strip")
        self.enable("poster_refresh")
        r = await self.http.delete(f"/Users/u/FavoriteItems/{ID1}")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.content, b"")
        r = await self.http.get("/Items/zzz/Images/Primary")
        self.assertEqual((r.status_code, r.text), (404, "nope"))
        r = await self.http.get("/text")
        self.assertEqual(r.text, "plain text")
        r = await self.http.head("/Videos/x/stream")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"")
        r = await self.http.get("/redir")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/Items?x=1")

    async def test_duplicate_headers_preserved(self):
        r = await self.http.get("/cookies")
        self.assertEqual(sorted(r.headers.get_list("set-cookie")), ["a=1; Path=/", "b=2; Path=/"])

    async def test_upstream_down_is_502(self):
        class Boom(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("down")

        await self.shutdown()
        await self.boot(Boom())
        r = await self.http.get("/Items")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.ctx.proxy_stats["upstream_errors"], 1)

    async def test_malformed_json_passes_through(self):
        self.enable("stream_strip")
        # /text is not JSON; /echo returns JSON we can corrupt via transport
        class Bad(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{oops")

        await self.shutdown()
        await self.boot(Bad())
        self.enable("stream_strip")
        r = await self.http.get("/anything")
        self.assertEqual(r.content, b"{oops")


# ===========================================================================
class StreamStripTests(Base):
    async def test_strips_quality_fields_keeps_labels(self):
        self.enable("stream_strip")
        items = await self.items()
        v, a = items[0]["MediaSources"][0]["MediaStreams"]
        for gone in ("Codec", "Width", "Height", "BitRate", "BitDepth", "VideoRangeType"):
            self.assertNotIn(gone, v)
        for gone in ("Channels", "ChannelLayout", "SampleRate"):
            self.assertNotIn(gone, a)
        self.assertEqual(v["DisplayTitle"], "4K HEVC")
        self.assertEqual(v["Language"], "eng")
        self.assertEqual(v["Index"], 0)
        self.assertEqual(items[0]["Name"], "Alpha")
        st = self.ctx.strip_stats
        self.assertEqual(st["responses_stripped"], 1)
        self.assertEqual(st["streams_stripped"], 6)
        self.assertGreater(st["fields_removed"], 10)

    async def test_playbackinfo_post_and_event(self):
        self.enable("stream_strip")
        r = await self.http.post(f"/Items/{ID1}/PlaybackInfo", json={})
        s = r.json()["MediaSources"][0]["MediaStreams"][0]
        self.assertNotIn("Codec", s)
        self.assertTrue(any(e["cat"] == "STRIP" for e in self.ctx.events.since(0)))

    async def test_scope_playback_only(self):
        self.enable("stream_strip", scope="playback")
        items = await self.items()
        self.assertIn("Codec", items[0]["MediaSources"][0]["MediaStreams"][0])
        r = await self.http.get(f"/Items/{ID1}/PlaybackInfo")
        self.assertNotIn("Codec", r.json()["MediaSources"][0]["MediaStreams"][0])

    async def test_custom_field_list_is_live(self):
        self.enable("stream_strip", fields=["Codec"])
        v = (await self.items())[0]["MediaSources"][0]["MediaStreams"][0]
        self.assertNotIn("Codec", v)
        self.assertIn("Width", v)
        self.ctx.settings.update("stream_strip", {"fields": ["width"]})  # case-insensitive, no restart
        v = (await self.items())[0]["MediaSources"][0]["MediaStreams"][0]
        self.assertIn("Codec", v)
        self.assertNotIn("Width", v)

    async def test_disable_restores_passthrough(self):
        self.enable("stream_strip")
        self.ctx.settings.update("stream_strip", {"enabled": False})
        v = (await self.items())[0]["MediaSources"][0]["MediaStreams"][0]
        self.assertIn("Codec", v)


# ===========================================================================
class PosterTests(Base):
    async def test_first_sighting_is_silent(self):
        self.poster_fast()
        items = await self.items()
        orig = self.mock.tags[(ID1, "Primary")]
        self.assertEqual(self.primary(items, ID1), orig)
        await self.wait_for(lambda: self.ctx.poster.stats["checks_ok"] >= 2)
        items = await self.items()
        self.assertEqual(self.primary(items, ID1), orig)          # baseline did not change tags
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 0)
        self.assertEqual(self.ctx.poster.stats["tags_rewritten"], 0)

    async def test_item_and_image_type_filters(self):
        self.poster_fast()
        await self.baseline()
        keys = set(self.ctx.poster.records)
        self.assertEqual(keys, {(ID1, "Primary"), (ID2, "Primary")})   # Episode + Backdrop not watched
        self.poster_fast(image_types=["Primary", "Backdrop"], item_types=["Movie", "Series", "Episode"])
        await self.items()
        self.assertIn((ID1, "Backdrop"), self.ctx.poster.records)
        self.assertIn((ID3, "Primary"), self.ctx.poster.records)

    async def test_detects_real_change_only(self):
        self.poster_fast()
        await self.baseline()
        orig1, orig2 = self.mock.tags[(ID1, "Primary")], self.mock.tags[(ID2, "Primary")]

        await self.recheck()                                   # nothing changed
        items = await self.items()
        self.assertEqual(self.primary(items, ID1), orig1)
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 0)

        self.mock.swap_image(ID1)                              # addon swaps poster 1
        await self.recheck()
        items = await self.items()
        t1 = self.primary(items, ID1)
        self.assertNotEqual(t1, orig1)
        self.assertEqual(len(t1), 32)
        self.assertEqual(self.primary(items, ID2), orig2)      # untouched item keeps tag
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 1)

        await self.recheck()                                   # unchanged again -> tag is stable
        self.assertEqual(self.primary(await self.items(), ID1), t1)
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 1)

    async def test_cooldown_holds_back_churn(self):
        self.poster_fast()
        await self.baseline()
        self.mock.swap_image(ID1, seed=50)
        await self.recheck()
        t1 = self.primary(await self.items(), ID1)
        self.mock.swap_image(ID1, seed=51)                     # rating ticked: pixels differ again
        await self.recheck()
        self.assertEqual(self.primary(await self.items(), ID1), t1)
        self.assertEqual(self.ctx.poster.stats["changes_held_by_cooldown"], 1)
        self.ctx.settings.update("poster_refresh", {"min_change_interval_hours": 0})
        await self.recheck()                                   # cooldown over: change lands
        self.assertNotEqual(self.primary(await self.items(), ID1), t1)
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 2)

    async def test_remux_own_tag_change_passes_through_without_double_bump(self):
        self.poster_fast()
        await self.baseline()
        self.mock.tags[(ID1, "Primary")] = "99999999-aaaa-bbbb-cccc-999999999999"
        self.mock.swap_image(ID1, seed=70)                     # Remux re-imported: new row, new bytes
        items = await self.items()
        self.assertEqual(self.primary(items, ID1), "99999999-aaaa-bbbb-cccc-999999999999")
        await self.recheck()
        self.assertEqual(self.primary(await self.items(), ID1), "99999999-aaaa-bbbb-cccc-999999999999")
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 0)

    async def test_state_survives_restart(self):
        self.poster_fast()
        await self.baseline()
        self.mock.swap_image(ID1)
        await self.recheck()
        t1 = self.primary(await self.items(), ID1)
        await self.shutdown()
        await self.boot()
        self.assertTrue(self.ctx.settings.get("poster_refresh")["enabled"])
        self.assertEqual(self.primary(await self.items(), ID1), t1)
        self.assertEqual(self.ctx.poster.stats["changes_detected"], 0)   # restored, not re-detected

    async def test_force_refresh_and_purge(self):
        self.poster_fast()
        await self.baseline()
        orig = self.primary(await self.items(), ID1)
        await self.ctx.poster.force_refresh()
        forced = self.primary(await self.items(), ID1)
        self.assertNotEqual(forced, orig)
        self.assertEqual(self.primary(await self.items(), ID1), forced)   # stable
        n = await self.ctx.poster.purge()
        self.assertEqual(n, 2)
        self.assertEqual(self.primary(await self.items(), ID1), orig)

    async def test_failed_checks_never_change_tags(self):
        self.poster_fast()
        await self.baseline()
        del self.mock.images[(ID1, "Primary")]                 # image now 404s
        await self.recheck()
        self.assertGreaterEqual(self.ctx.poster.stats["checks_failed"], 1)
        self.assertEqual(self.primary(await self.items(), ID1), self.mock.tags[(ID1, "Primary")])
        rec = self.ctx.poster.records[(ID1, "Primary")]
        self.assertEqual(rec.fail_count, 1)
        self.assertGreater(rec.next_check, time.time())        # backed off

    async def test_disabled_module_does_not_probe_or_rewrite(self):
        self.poster_fast()
        self.ctx.settings.update("poster_refresh", {"enabled": False})
        await self.items()
        await asyncio.sleep(0.2)
        self.assertEqual(self.ctx.poster.records, {})

    async def test_backdrop_rewrite_in_both_places(self):
        self.poster_fast(image_types=["Backdrop"], item_types=["Movie"])
        await self.items()
        await self.wait_for(lambda: self.ctx.poster.stats["checks_ok"] >= 1)
        self.mock.swap_image(ID1, "Backdrop", seed=33, size=(640, 360))
        await self.recheck(1)
        movie = next(i for i in await self.items() if i["Id"] == ID1)
        new = movie["ImageTags"]["Backdrop"]
        self.assertNotEqual(new, self.mock.tags[(ID1, "Backdrop")])
        self.assertEqual(movie["BackdropImageTags"], [new])

    async def test_parent_tags_on_children_follow_the_parent(self):
        self.poster_fast(image_types=["Primary", "Backdrop"], item_types=["Movie", "Series"])
        await self.items()
        await self.wait_for(lambda: self.ctx.poster.stats["checks_ok"] >= 3)
        await asyncio.sleep(0.05)
        self.mock.swap_image(ID2)                                  # series poster changes
        self.mock.swap_image(ID1, "Backdrop", seed=41, size=(640, 360))
        await self.recheck(3)
        items = await self.items()
        series = next(i for i in items if i["Id"] == ID2)
        movie = next(i for i in items if i["Id"] == ID1)
        ep = next(i for i in items if i["Id"] == ID3)              # Episode: not watched itself
        self.assertNotEqual(series["ImageTags"]["Primary"], self.mock.tags[(ID2, "Primary")])
        self.assertEqual(ep["SeriesPrimaryImageTag"], series["ImageTags"]["Primary"])
        self.assertEqual(ep["ParentBackdropImageTags"], movie["BackdropImageTags"])
        self.assertEqual(ep["ParentThumbImageTag"], movie["ImageTags"]["Backdrop"])
        self.assertEqual(ep["ImageTags"]["Primary"], self.mock.tags[(ID3, "Primary")])
        self.assertNotIn((ID3, "Primary"), self.ctx.poster.records)

    async def test_parent_tags_untouched_when_parent_not_tracked(self):
        self.poster_fast(item_types=["Movie"])                    # series not watched
        await self.ctx.poster.force_refresh()
        ep = next(i for i in await self.items() if i["Id"] == ID3)
        self.assertEqual(ep["SeriesPrimaryImageTag"], self.mock.tags[(ID2, "Primary")])

    async def test_dispatcher_survives_unexpected_errors(self):
        self.poster_fast()
        poster = self.ctx.poster
        real_spawn = poster._spawn
        calls = {"n": 0}

        def flaky_spawn(coro):
            calls["n"] += 1
            if calls["n"] == 1:
                coro.close()
                raise RuntimeError("boom")
            real_spawn(coro)

        poster._spawn = flaky_spawn
        await self.items()                                         # first key hits the error
        await asyncio.sleep(0.2)
        await self.recheck(1)                                      # loop still alive
        self.assertGreaterEqual(poster.stats["checks_ok"], 1)

    async def test_probe_now_diagnostic(self):
        self.poster_fast()
        await self.baseline()
        r = await self.ctx.poster.probe_now(ID1, "Primary")
        self.assertTrue(r["ok"])
        self.assertEqual(r["content_type"], "image/jpeg")
        self.assertTrue(r["matches_recorded"])
        bad = await self.ctx.poster.probe_now("44444444-4444-4444-4444-444444444444", "Primary")
        self.assertFalse(bad["ok"])


# ===========================================================================
class ImageTests(Base):
    async def get_img(self, iid=ID1, typ="Primary", **headers):
        return await self.http.get(f"/Items/{iid}/Images/{typ}", headers=headers)

    async def test_images_always_pass_through_untouched(self):
        self.poster_fast(log_image_requests=True)
        for typ in ("Primary", "Backdrop"):
            r = await self.get_img(typ=typ, **{"If-None-Match": "*"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, self.mock.images[(ID1, typ)])
            self.assertEqual(r.headers["cache-control"], "max-age=86400")   # Remux's own header
            self.assertNotIn("etag", r.headers)

    async def test_image_request_logging_is_opt_in(self):
        self.poster_fast()
        await self.get_img()
        self.assertFalse([e for e in self.ctx.events.since(0) if e["cat"] == "IMAGE"])
        self.ctx.settings.update("poster_refresh", {"log_image_requests": True})
        await self.http.get(f"/Items/{ID1}/Images/Primary?tag=abcdef0123456789")
        ev = [e for e in self.ctx.events.since(0) if e["cat"] == "IMAGE"]
        self.assertEqual(len(ev), 1)
        self.assertIn("tag=abcdef012345", ev[0]["msg"])


# ===========================================================================
class DiagnosticsTests(Base):
    def reqs(self):
        return [e for e in self.ctx.events.since(0) if e["cat"] == "REQUEST"]

    async def test_off_by_default_logs_nothing(self):
        await self.http.get("/Items?Limit=50")
        self.assertEqual(self.reqs(), [])

    async def test_logs_limit_and_counts_without_changing_the_response(self):
        plain = (await self.http.get("/Items?Limit=50&StartIndex=0")).content
        self.enable("diagnostics")
        r = await self.http.get("/Items?Limit=50&StartIndex=0")
        self.assertEqual(r.content, plain)                    # byte-identical
        self.assertEqual(r.headers["x-remux-test"], "yes")
        ev = self.reqs()
        self.assertEqual(len(ev), 1)
        msg = ev[0]["msg"]
        self.assertIn("GET /Items", msg)
        self.assertIn("Limit=50", msg)
        self.assertIn("StartIndex=0", msg)
        self.assertIn("items=3 total=3", msg)
        self.assertEqual(self.ctx.diag_stats["requests_logged"], 1)

    async def test_secrets_in_the_query_are_never_logged(self):
        self.enable("diagnostics")
        await self.http.get("/Items?Limit=5&api_key=SECRETKEY&X-Emby-Token=TOKEN123&ParentId=abc")
        msg = self.reqs()[0]["msg"]
        self.assertNotIn("SECRETKEY", msg)
        self.assertNotIn("TOKEN123", msg)
        self.assertIn("ParentId=abc", msg)

    async def test_scope_lists_skips_non_lists_and_all_json_includes_them(self):
        self.enable("diagnostics")
        await self.http.get("/Items/x/PlaybackInfo")
        self.assertEqual(self.reqs(), [])
        self.ctx.settings.update("diagnostics", {"scope": "all_json"})
        await self.http.get("/Items/x/PlaybackInfo")
        self.assertEqual(len(self.reqs()), 1)

    async def test_works_alongside_modules_and_does_not_disable_them(self):
        self.enable("diagnostics")
        self.enable("stream_strip")
        r = await self.http.get("/Items/x/PlaybackInfo")
        self.assertNotIn("Width", r.text)                     # strip still applied
        self.enable("poster_refresh")
        await self.http.get("/Items")
        self.assertTrue(self.reqs())

    async def test_images_are_not_buffered_or_logged(self):
        self.enable("diagnostics")
        r = await self.http.get(f"/Items/{ID1}/Images/Primary")
        self.assertEqual(r.content, self.mock.images[(ID1, "Primary")])
        self.assertEqual(self.reqs(), [])


# ===========================================================================
class SeeAllTests(Base):
    SEE_ALL = "/Items/Latest?parentId=abc&startIndex=0&limit=50&fields=Overview"

    def last_query(self):
        return self.mock.seen_queries[-1]

    async def infuse_list(self, parent="abc", limit=50):
        """Behave like Infuse: take the first response's length as the total, ask
        for every remaining page, and stack all pages after the first one."""
        first = (await self.http.get(
            f"/Items/Latest?parentId={parent}&startIndex=0&limit={limit}&fields=Overview")).json()
        total = len(first)
        stacked = list(first)
        pages = max(0, -(-total // limit) - 1)
        for i in range(1, pages + 1):
            start = i * limit
            r = await self.http.get(
                f"/Items/Latest?parentId={parent}&startIndex={start}&limit={min(limit, total - start)}")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json(), f"page {i} must never be empty")
            self.assertLessEqual(len(r.json()), min(limit, total - start))
            stacked += r.json()
        return first, stacked

    def ids(self, rows):
        return [r["Id"] for r in rows]

    async def test_off_by_default_passes_limit_through(self):
        r = await self.http.get(self.SEE_ALL)
        self.assertEqual(len(r.json()), 50)
        self.assertIn("limit=50", self.last_query())

    async def test_whole_catalog_arrives_once_in_order(self):
        self.enable("see_all")
        for total in (51, 52, 53, 99, 100, 101, 120, 251, 252, 253, 300, 492, 493, 499):
            self.mock.latest_total = total
            self.ctx.see_all_served.clear()
            first, stacked = await self.infuse_list()
            want = [f"{i:032x}" for i in range(total)]
            if total == 51:        # no clean split: plain full list, nothing missing
                self.assertEqual(self.ids(first), want)
                continue
            self.assertLess(len(first), total, total)
            self.assertEqual(self.ids(stacked), want, total)   # complete, unique, in order

    async def test_first_request_asks_remux_for_everything(self):
        self.mock.latest_total = 300
        self.enable("see_all")
        await self.http.get(self.SEE_ALL)
        self.assertIn("limit=10000", self.last_query())
        self.assertIn("parentId=abc", self.last_query())         # rest of the query untouched
        self.assertIn("fields=Overview", self.last_query())
        self.assertEqual(self.ctx.see_all_stats["requests_expanded"], 1)
        self.assertEqual(self.ctx.see_all_stats["last_items_returned"], 300)

    def checks(self):
        st = self.ctx.poster.stats
        return st["checks_ok"] + st["checks_failed"]

    async def test_poster_checks_follow_what_is_delivered(self):
        """Opening a long 'see all' must not queue a check for the held-back
        pages; each is queued only when Infuse asks for it."""
        self.mock.latest_total = 300
        self.enable("see_all")
        self.enable("poster_refresh", probe_concurrency=8, item_types=["Movie"])
        first = (await self.http.get(self.SEE_ALL)).json()
        await self.wait_for(lambda: self.checks() >= len(first))
        await asyncio.sleep(0.3)
        self.assertEqual(self.checks(), len(first))            # not 300
        sent = len(first)
        start = 50
        page = await self.http.get(f"/Items/Latest?parentId=abc&startIndex={start}&limit=50")
        sent += len(page.json())
        await self.wait_for(lambda: self.checks() >= sent)
        await asyncio.sleep(0.2)
        self.assertEqual(self.checks(), sent)
        # reading the rest checks the rest
        total = len(first)
        start = 100
        while sent < 300:
            page = (await self.http.get(
                f"/Items/Latest?parentId=abc&startIndex={start}&limit={min(50, 300 - start)}")).json()
            sent += len(page)
            start += 50
        await self.wait_for(lambda: self.checks() >= 300)
        self.assertEqual(self.checks(), 300)
        self.assertEqual(self.ctx.see_all_pending["abc"], {})

    async def test_unsplit_see_all_checks_everything_delivered(self):
        self.mock.latest_total = 51                            # no clean split: sent whole
        self.enable("see_all")
        self.enable("poster_refresh", probe_concurrency=8, item_types=["Movie"])
        first = (await self.http.get(self.SEE_ALL)).json()
        await self.wait_for(lambda: self.checks() >= len(first))
        self.assertEqual(self.checks(), 51)

    async def test_follow_up_pages_never_reach_remux(self):
        self.mock.latest_total = 300
        self.enable("see_all")
        first, _ = await self.infuse_list()
        seen = len(self.mock.seen_queries)
        await self.http.get("/Items/Latest?parentId=abc&startIndex=50&limit=50")
        self.assertEqual(len(self.mock.seen_queries), seen)
        self.assertGreaterEqual(self.ctx.see_all_stats["pages_served"], 1)

    async def test_cap_overrides_everything(self):
        self.mock.latest_total = 300
        self.enable("see_all", max_items=75)
        first, stacked = await self.infuse_list()
        self.assertEqual(len(stacked), 75)
        await self.http.get(self.SEE_ALL)
        self.assertIn("limit=75", self.last_query())

    async def test_home_rows_and_other_sizes_are_not_touched(self):
        self.mock.latest_total = 300
        self.enable("see_all")
        for q, n in (("limit=20", 20), ("limit=51", 51), ("limit=500", 300), ("parentId=x", 20)):
            r = await self.http.get("/Items/Latest?" + q)
            self.assertEqual(len(r.json()), n, q)
        self.assertEqual(self.ctx.see_all_stats["requests_expanded"], 0)
        self.assertNotIn("limit=10000", "".join(self.mock.seen_queries))

    async def test_only_the_latest_endpoint_is_expanded(self):
        self.enable("see_all")
        await self.http.get("/Items?limit=50&filters=IsFavorite")
        self.assertIn("limit=50", self.last_query())
        self.assertEqual(self.ctx.see_all_stats["requests_expanded"], 0)

    async def test_works_with_user_prefixed_path_and_case(self):
        self.mock.latest_total = 200
        self.enable("see_all")
        first = (await self.http.get("/Users/u1/Items/Latest?Limit=50")).json()
        self.assertEqual(len(first), 125)                         # first part; rest comes in pages
        page = (await self.http.get("/Users/u1/Items/Latest?startIndex=50&Limit=50")).json()
        self.assertEqual(self.ids(page), [f"{i:032x}" for i in range(125, 175)])

    async def test_cap_at_or_below_trigger_does_nothing(self):
        self.mock.latest_total = 300
        self.enable("see_all", max_items=40)
        r = await self.http.get(self.SEE_ALL)
        self.assertEqual(len(r.json()), 50)

    async def test_custom_trigger_size(self):
        self.mock.latest_total = 300
        self.enable("see_all", trigger_limit=30)
        first, stacked = await self.infuse_list(limit=30)
        self.assertEqual(self.ids(stacked), [f"{i:032x}" for i in range(300)])
        self.assertEqual(len((await self.http.get("/Items/Latest?limit=50")).json()), 50)

    async def test_catalogs_are_kept_separately_and_latest_replaces_older(self):
        self.mock.latest_total = 200
        self.enable("see_all")
        await self.infuse_list(parent="a")
        await self.infuse_list(parent="b")
        self.assertEqual(set(self.ctx.see_all_served), {"a", "b"})
        self.mock.latest_total = 160
        first, stacked = await self.infuse_list(parent="a")        # "see all" again: fresh split
        self.assertEqual(len(stacked), 160)
        for i in range(30):
            self.ctx.see_all_served.pop(f"p{i}", None)
            await self.http.get(f"/Items/Latest?parentId=p{i}&startIndex=0&limit=50")
        self.assertLessEqual(len(self.ctx.see_all_served), 20)       # bounded, no timer needed

    async def test_unknown_catalog_page_goes_to_remux(self):
        self.mock.latest_total = 300
        self.enable("see_all")
        r = await self.http.get("/Items/Latest?parentId=never&startIndex=50&limit=50")
        self.assertEqual(len(r.json()), 50)
        self.assertEqual(self.ctx.see_all_stats["pages_served"], 0)

    async def test_pages_follow_other_modules_rewrites(self):
        self.mock.latest_total = 200
        self.enable("see_all")
        self.enable("poster_refresh")
        first, stacked = await self.infuse_list()
        self.assertEqual(self.ids(stacked), [f"{i:032x}" for i in range(200)])

    def test_plan_pages_always_splits_cleanly(self):
        from refused.proxy import plan_pages
        for page in (20, 30, 50):
            for n in range(page + 2, 1500):
                k, slices = plan_pages(n, page)
                m = (k - 1) // page
                self.assertEqual(len(slices), m, (n, page))
                pos = k
                for i, (a, b) in enumerate(slices):
                    limit = page if i < m - 1 else k - page * m
                    self.assertEqual(a, pos)
                    self.assertTrue(1 <= b - a <= limit, (n, page, i))
                    pos = b
                self.assertEqual(pos, n, (n, page))                    # nothing lost, nothing repeated
        self.assertIsNone(plan_pages(51, 50))

    async def test_log_describes_the_split(self):
        self.mock.latest_total = 300
        self.enable("see_all")
        await self.http.get(self.SEE_ALL)
        msgs = [e["msg"] for e in self.ctx.events.since(0) if e["cat"] == "SEE_ALL"]
        self.assertTrue(any("300 items (300 unique)" in m for m in msgs))
        self.assertTrue(any(m.startswith("sent the first 175 now; the other 125 follow in 3 pages") for m in msgs))
        self.assertTrue(any(m.startswith("list: 300 items, 300 different titles, 0 repeat") for m in msgs))

    async def test_log_names_repeated_titles(self):
        from refused.proxy import list_summary
        rows = [{"Id": "a", "Name": "Wednesday", "Type": "Series", "ProductionYear": 2022},
                {"Id": "b", "Name": "Dune", "Type": "Movie", "ProductionYear": 2021},
                {"Id": "c", "Name": "Wednesday", "Type": "Series", "ProductionYear": 2022},
                {"Id": "d", "Name": "Pilot", "SeriesName": "Show", "Type": "Episode"}]
        lines = list_summary(rows)
        self.assertIn("4 items, 3 different titles, 1 repeat", lines[0])
        self.assertIn("#3 Wednesday (2022) (also #1)", lines[1])
        self.assertTrue(lines[2].startswith("first 8: #1 Wednesday (2022) | #2 Dune (2021)"))
        self.assertIn("#4 Show - Pilot", lines[3])
        self.assertEqual(list_summary([])[0], "list: 0 items, 0 different titles, 0 repeat an earlier title")

    async def test_settings_validation_and_expand_helper(self):
        from refused.proxy import expand_limit
        with self.assertRaises(ValidationError):
            self.ctx.settings.update("see_all", {"max_items": -1})
        with self.assertRaises(ValidationError):
            self.ctx.settings.update("see_all", {"trigger_limit": 0})
        self.assertEqual(expand_limit(b"a=1&LIMIT=50&b=2", 50, 9), (b"a=1&LIMIT=9&b=2", True))
        self.assertEqual(expand_limit(b"limit=500", 50, 9), (b"limit=500", False))
        self.assertEqual(expand_limit(b"limitx=50", 50, 9), (b"limitx=50", False))


# ===========================================================================
class SearchCleanTests(Base):
    async def test_off_by_default_people_pass_through(self):
        r = await self.http.get("/Persons?searchTerm=Will&limit=24")
        self.assertEqual(r.json()["TotalRecordCount"], 1)

    async def test_people_search_is_answered_empty_without_asking_remux(self):
        self.enable("search_clean")
        before = len(self.mock.seen_queries)
        r = await self.http.get("/Persons?searchTerm=Will%20smith&recursive=true&limit=24")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
        self.assertEqual(len(self.mock.seen_queries), before)
        self.assertEqual(self.ctx.search_stats["people_searches_hidden"], 1)
        self.assertTrue([e for e in self.ctx.events.since(0) if e["cat"] == "SEARCH_CLEAN"])

    async def test_titles_and_browsing_are_untouched(self):
        self.enable("search_clean")
        r = await self.http.get("/Items?searchTerm=Will&recursive=true&limit=24")
        self.assertEqual(len(r.json()["Items"]), 3)                 # title search unchanged
        r = await self.http.get("/Persons?limit=24")                 # no search term: not a search
        self.assertEqual(r.json()["TotalRecordCount"], 1)
        r = await self.http.get("/Persons?searchTerm=")              # empty term: not a search
        self.assertEqual(r.json()["TotalRecordCount"], 1)
        self.assertEqual(self.ctx.search_stats["people_searches_hidden"], 0)

    async def test_choice_can_be_cleared(self):
        self.enable("search_clean", hide=[])
        r = await self.http.get("/Persons?searchTerm=Will")
        self.assertEqual(r.json()["TotalRecordCount"], 1)


# ===========================================================================
class FirstRunDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_module_has_a_first_run_env_default(self):
        import os
        from unittest import mock as umock
        env = {"REFUSED_USERNAME": "a", "REFUSED_PASSWORD": "b",
               **{f"{m.upper()}_DEFAULT": "true" for m in
                  ("stream_strip", "poster_refresh", "see_all", "search_clean", "diagnostics")}}
        with umock.patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as d:
            os.environ["DATA_DIR"] = d
            cfg = Config.from_env()
            app = create_app(cfg, upstream_transport=httpx.ASGITransport(app=MockRemux().app))
            async with app.router.lifespan_context(app):
                for m in ("stream_strip", "poster_refresh", "see_all", "search_clean", "diagnostics"):
                    self.assertTrue(app.state.ctx.settings.get(m)["enabled"], m)


# ===========================================================================
class SettingsMigrationTests(unittest.TestCase):
    def store(self, saved):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "settings.json"
        if saved is not None:
            path.write_text(json.dumps(saved))
        from refused.settings import SettingsStore
        return SettingsStore(path), path

    def test_old_default_of_three_becomes_sixteen_once(self):
        st, path = self.store({"poster_refresh": {"enabled": True, "probe_concurrency": 3,
                                                  "probes_per_minute": 30}})
        self.assertEqual(st.get("poster_refresh")["probe_concurrency"], 16)
        self.assertTrue(st.get("poster_refresh")["enabled"])
        self.assertNotIn("probes_per_minute", st.get("poster_refresh"))
        st.update("poster_refresh", {"probe_concurrency": 3})   # a deliberate 3 sticks
        from refused.settings import SettingsStore
        self.assertEqual(SettingsStore(path).get("poster_refresh")["probe_concurrency"], 3)

    def test_other_saved_values_are_kept(self):
        st, _ = self.store({"poster_refresh": {"probe_concurrency": 5}})
        self.assertEqual(st.get("poster_refresh")["probe_concurrency"], 5)

    def test_fresh_install_defaults_and_range(self):
        st, _ = self.store(None)
        self.assertEqual(st.get("poster_refresh")["probe_concurrency"], 16)
        st.update("poster_refresh", {"probe_concurrency": 64})
        with self.assertRaises(Exception):
            st.update("poster_refresh", {"probe_concurrency": 65})
        with self.assertRaises(Exception):
            st.update("poster_refresh", {"probes_per_minute": 30})   # setting is gone


# ===========================================================================
class AdminTests(Base):
    async def test_requires_login(self):
        for method, path in [("GET", "/refused/api/state"), ("GET", "/refused/api/events"),
                             ("PUT", "/refused/api/settings/poster_refresh"),
                             ("POST", "/refused/api/poster/purge"),
                             ("POST", "/refused/api/poster/force-refresh"),
                             ("POST", "/refused/api/poster/probe")]:
            r = await self.http.request(method, path, json={})
            self.assertEqual(r.status_code, 401, path)

    async def test_bad_login_and_good_login(self):
        r = await self.http.post("/refused/api/login", json={"username": USER, "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("set-cookie", r.headers)
        r = await self.http.post("/refused/api/login", json={"username": "nope", "password": PASSWORD})
        self.assertEqual(r.status_code, 401)
        r = await self.http.post("/refused/api/login", json={"username": 5})
        self.assertEqual(r.status_code, 400)
        csrf = await self.login()
        cookie = self.http.cookies.get("refused_session")
        self.assertTrue(cookie)
        s = (await self.http.get("/refused/api/session")).json()
        self.assertTrue(s["authenticated"])
        self.assertEqual(s["csrf"], csrf)

    async def test_cookie_flags(self):
        r = await self.http.post("/refused/api/login", json={"username": USER, "password": PASSWORD})
        sc = r.headers["set-cookie"].lower()
        self.assertIn("httponly", sc)
        self.assertIn("samesite=strict", sc)
        self.assertIn("path=/refused", sc)

    async def test_tampered_or_foreign_cookie_rejected(self):
        await self.login()
        good = self.http.cookies.get("refused_session")
        payload, _, sig = good.rpartition(".")
        for bad in (payload + "." + "0" * len(sig), "garbage", payload + "x." + sig, ""):
            self.http.cookies.set("refused_session", bad, path="/refused")
            r = await self.http.get("/refused/api/state")
            self.assertEqual(r.status_code, 401, bad)

    async def test_session_dies_when_password_changes(self):
        await self.login()
        cookie = self.http.cookies.get("refused_session")
        await self.shutdown()
        cfg = Config(remux_url="http://remux.test", username=USER, password="a-new-password",
                     data_dir=Path(self.tmp.name))
        self.app = create_app(cfg, upstream_transport=httpx.ASGITransport(app=self.mock.app))
        self._cm = self.app.router.lifespan_context(self.app)
        await self._cm.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://refused.test")
        self._up = True
        self.http.cookies.set("refused_session", cookie, path="/refused")
        self.assertEqual((await self.http.get("/refused/api/state")).status_code, 401)

    async def test_session_survives_restart_same_password(self):
        await self.login()
        cookie = self.http.cookies.get("refused_session")
        await self.shutdown()
        await self.boot()
        self.http.cookies.set("refused_session", cookie, path="/refused")
        self.assertEqual((await self.http.get("/refused/api/state")).status_code, 200)

    async def test_login_throttle(self):
        for _ in range(5):
            r = await self.http.post("/refused/api/login", json={"username": USER, "password": "bad"})
            self.assertEqual(r.status_code, 401)
        r = await self.http.post("/refused/api/login", json={"username": USER, "password": PASSWORD})
        self.assertEqual(r.status_code, 429)   # even the right password is refused while locked
        self.assertGreater(int(r.headers["retry-after"]), 0)

    async def test_oversized_admin_body_rejected(self):
        csrf = await self.login()
        r = await self.http.put("/refused/api/settings/stream_strip",
                                content=b'{"fields":["' + b"a" * 70000 + b'"]}',
                                headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)

    async def test_csrf_required_for_writes(self):
        csrf = await self.login()
        r = await self.http.put("/refused/api/settings/stream_strip", json={"enabled": True})
        self.assertEqual(r.status_code, 403)
        r = await self.http.put("/refused/api/settings/stream_strip", json={"enabled": True},
                                headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.ctx.settings.get("stream_strip")["enabled"])
        r = await self.http.put("/refused/api/settings/stream_strip", json={"enabled": True},
                                headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.ctx.settings.get("stream_strip")["enabled"])

    async def test_toggle_takes_effect_immediately_on_proxy(self):
        csrf = await self.login()
        h = {"X-CSRF-Token": csrf}
        self.assertIn("Codec", (await self.items())[0]["MediaSources"][0]["MediaStreams"][0])
        await self.http.put("/refused/api/settings/stream_strip", json={"enabled": True}, headers=h)
        self.assertNotIn("Codec", (await self.items())[0]["MediaSources"][0]["MediaStreams"][0])
        await self.http.put("/refused/api/settings/stream_strip", json={"enabled": False}, headers=h)
        self.assertIn("Codec", (await self.items())[0]["MediaSources"][0]["MediaStreams"][0])

    async def test_settings_validation_and_reset(self):
        csrf = await self.login()
        h = {"X-CSRF-Token": csrf}
        r = await self.http.put("/refused/api/settings/poster_refresh",
                                json={"recheck_hours": 0, "image_types": ["Banner"]}, headers=h)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(len(r.json()["errors"]), 2)
        r = await self.http.put("/refused/api/settings/poster_refresh", json={"recheck_hours": 5}, headers=h)
        self.assertEqual(r.json()["settings"]["recheck_hours"], 5)
        r = await self.http.put("/refused/api/settings/poster_refresh", json={"reset": True}, headers=h)
        self.assertEqual(r.json()["settings"]["recheck_hours"], 24)
        r = await self.http.put("/refused/api/settings/nope", json={}, headers=h)
        self.assertEqual(r.status_code, 404)

    async def test_state_payload(self):
        await self.login()
        s = (await self.http.get("/refused/api/state")).json()
        self.assertEqual(set(s["modules"]), {"stream_strip", "poster_refresh", "see_all", "search_clean", "diagnostics"})
        self.assertEqual(set(s["schema"]), {"stream_strip", "poster_refresh", "see_all", "search_clean", "diagnostics"})
        self.assertTrue(s["upstream"]["reachable"])
        self.assertEqual(s["upstream"]["version"], "0")          # Remux's own version
        self.assertEqual(s["upstream"]["server_name"], "mock")
        for mod in s["schema"].values():
            for f in mod["fields"]:
                self.assertTrue(f["help"], f["key"])         # every setting is described
                self.assertTrue(f["label"])

    async def test_poster_endpoints(self):
        csrf = await self.login()
        h = {"X-CSRF-Token": csrf}
        self.poster_fast()
        await self.baseline()
        r = await self.http.post("/refused/api/poster/probe", json={"item_id": ID1, "image_type": "Primary"}, headers=h)
        j = r.json()
        self.assertTrue(j["probe"]["ok"])
        self.assertEqual(j["records"][0]["image_type"], "Primary")
        for bad in ({"item_id": "../etc/passwd"}, {"item_id": ID1, "image_type": "Banner"}, {}):
            r = await self.http.post("/refused/api/poster/probe", json=bad, headers=h)
            self.assertEqual(r.status_code, 422, bad)
        r = await self.http.post("/refused/api/poster/force-refresh", json={}, headers=h)
        self.assertEqual(r.json()["epoch"], 1)
        r = await self.http.post("/refused/api/poster/purge", json={}, headers=h)
        self.assertEqual(r.json()["forgotten"], 2)

    async def test_event_log_endpoint(self):
        await self.login()
        j = (await self.http.get("/refused/api/events")).json()
        self.assertTrue(any(e["cat"] == "AUTH" for e in j["events"]))
        again = (await self.http.get(f"/refused/api/events?after={j['last_id']}")).json()
        self.assertEqual(again["events"], [])

    async def test_logout(self):
        csrf = await self.login()
        r = await self.http.post("/refused/api/logout", json={}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.http.cookies.clear()
        self.assertEqual((await self.http.get("/refused/api/state")).status_code, 401)

    async def test_pages_and_security_headers(self):
        for path in ("/refused", "/refused/"):
            r = await self.http.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertIn("REFUSED", r.text)
            self.assertIn("default-src 'self'", r.headers["content-security-policy"])
            self.assertEqual(r.headers["x-frame-options"], "DENY")
        js = await self.http.get("/refused/static/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        css = await self.http.get("/refused/static/app.css")
        self.assertIn("text/css", css.headers["content-type"])
        r304 = await self.http.get("/refused/static/app.css", headers={"If-None-Match": css.headers["etag"]})
        self.assertEqual(r304.status_code, 304)
        self.assertEqual((await self.http.get("/refused/static/secret.py")).status_code, 404)
        self.assertEqual((await self.http.get("/refused/static/..%2fconfig.py")).status_code, 404)

    async def test_healthz_is_public_and_minimal(self):
        r = await self.http.get("/refused/healthz")
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["modules"],
                         {"stream_strip": False, "poster_refresh": False,
                          "see_all": False, "search_clean": False, "diagnostics": False})
        self.assertNotIn("password", r.text.lower())

    async def test_admin_does_not_shadow_remux_paths(self):
        r = await self.http.get("/Items")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Items", r.json())


if __name__ == "__main__":
    unittest.main()
