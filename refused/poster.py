"""POSTER_REFRESH: tell Infuse when artwork really changed, and keep it small.

How it works
------------
Remux reports every image as ``ImageTags: {"Primary": "<image row uuid>"}``.
That uuid stays the same when an addon serves a different picture at the same
URL, so Infuse (which only re-downloads when the tag changes) never notices.

For each (item, image type) that passes the filters, this module:

1. remembers Remux's own tag and a SHA-256 of the image bytes ("fingerprint");
2. re-fetches the image in the background on a schedule and compares;
3. when the bytes changed (and the cooldown allows it) bumps a version
   counter, which changes the tag it *emits* to Infuse.

The emitted tag is a pure function of (Remux's tag, version, epoch):

* version == 0 and epoch == 0  ->  Remux's tag, untouched
* otherwise                    ->  sha1(orig|version|epoch)[:32]

So unchanged artwork always yields the same tag (no duplicate cache entries),
the first sighting of an image is recorded silently (no mass re-download when
the module is switched on), and a restart cannot flip tags.

The same tag is also applied wherever Remux repeats a parent's image tag on a
child (``SeriesPrimaryImageTag`` on seasons/episodes, ``ParentBackdropImageTags``
...), so one picture never ends up cached under two tags.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from .poster_store import Key, PosterStore, Rec

log = logging.getLogger("refused.poster")

IMAGE_PATH_RE = re.compile(
    r"^/items/([0-9a-fA-F-]{32,36})/images/([A-Za-z]+)(?:/\d+)?/?$", re.IGNORECASE
)
CANONICAL_TYPES = {"primary": "Primary", "backdrop": "Backdrop", "logo": "Logo", "thumb": "Thumb"}

# Child fields that repeat a parent's image tag: (tag field, parent-id field,
# image types the tag may belong to, in lookup order). Remux falls back from a
# series' Thumb to its Backdrop for ParentThumbImageTag, hence two types there.
PARENT_TAG_FIELDS = (
    ("SeriesPrimaryImageTag", "SeriesId", ("Primary",)),
    ("ParentPrimaryImageTag", "ParentPrimaryImageItemId", ("Primary",)),
    ("ParentThumbImageTag", "ParentThumbItemId", ("Thumb", "Backdrop")),
    ("ParentLogoImageTag", "ParentLogoItemId", ("Logo",)),
)

MAX_QUEUE = 20000
FLUSH_SECONDS = 5


def match_image_path(path: str) -> tuple[str, str] | None:
    """('item-id', 'Primary') for /Items/{id}/Images/{type}[/{index}], else None."""
    m = IMAGE_PATH_RE.match(path)
    if not m:
        return None
    canon = CANONICAL_TYPES.get(m.group(2).lower())
    return (m.group(1), canon) if canon else None


def make_tag(orig: str, version: int, epoch: int) -> str:
    if version == 0 and epoch == 0:
        return orig
    return hashlib.sha1(f"{orig}|{version}|{epoch}".encode()).hexdigest()[:32]


class ProbeError(Exception):
    pass


class PosterModule:
    name = "poster_refresh"

    def __init__(self, ctx):
        self.ctx = ctx
        self.records: dict[Key, Rec] = {}
        self.epoch = 0
        self.stats: dict[str, int] = {
            "tags_rewritten": 0,
            "checks_ok": 0,
            "checks_failed": 0,
            "changes_detected": 0,
            "changes_held_by_cooldown": 0,
        }
        self._store: PosterStore | None = None
        self._dirty: set[Key] = set()
        self._queued: set[Key] = set()
        self._queue: asyncio.Queue[Key] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._inflight = 0

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._store = PosterStore(self.ctx.config.data_dir / "refused.db")
        self.records, self.epoch = self._store.load_all()
        if self.records:
            self.ctx.events.info(
                "POSTER", f"restored {len(self.records)} tracked images (epoch {self.epoch})"
            )
        self._spawn(self._dispatch_loop())
        self._spawn(self._flush_loop())

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.flush()
        finally:
            if self._store:
                self._store.close()
                self._store = None

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---- persistence -----------------------------------------------------------
    async def flush(self) -> None:
        if not self._dirty or not self._store:
            return
        keys = list(self._dirty)
        self._dirty.clear()
        rows = [r.row(k) for k in keys if (r := self.records.get(k))]
        try:
            await asyncio.to_thread(self._store.upsert, rows)
        except Exception:
            self._dirty.update(keys)
            log.exception("could not save poster state; will retry")

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            await self.flush()

    # ---- tag rewriting (hot path: runs on every JSON response) -------------------
    def resolve(self, item_id: str, img_type: str, orig_tag: str,
                sink: dict | None = None) -> str:
        """Tag to emit for one image; records first sightings and schedules checks.

        With ``sink`` the check is not queued now; the key is parked in
        ``sink[item_id]`` for ``release`` to queue once the item is delivered."""
        key = (item_id, img_type)
        rec = self.records.get(key)
        if rec is None:
            rec = self.records[key] = Rec(orig_tag=orig_tag)
            self._dirty.add(key)
        elif rec.orig_tag != orig_tag:
            # Remux itself replaced the image: its new tag propagates as-is and
            # the next check re-baselines without counting as a "change".
            rec.orig_tag = orig_tag
            rec.fp = None
            rec.next_check = 0.0
            self._dirty.add(key)
        if rec.next_check <= time.time():
            if sink is None:
                self._enqueue(key)
            else:
                sink.setdefault(item_id, []).append(key)
        return make_tag(orig_tag, rec.version, self.epoch)

    def _known(self, item_id: str, types: tuple[str, ...], orig_tag: str) -> str | None:
        """Emitted tag for a parent image we already track under this exact Remux
        tag; None if we do not (then the child keeps Remux's tag untouched)."""
        for img_type in types:
            rec = self.records.get((item_id, img_type))
            if rec is not None and rec.orig_tag == orig_tag:
                return make_tag(orig_tag, rec.version, self.epoch)
        return None

    def release(self, rows: list, sink: dict) -> int:
        """Queue the parked checks for the items in ``rows`` (the part of a list
        that was actually sent to the client). Returns how many were queued."""
        n = 0
        for row in rows:
            iid = row.get("Id") if isinstance(row, dict) else None
            for key in sink.pop(iid, ()) if isinstance(iid, str) else ():
                self._enqueue(key)
                n += 1
        return n

    def _enqueue(self, key: Key) -> None:
        if self._queue is None or key in self._queued or len(self._queued) >= MAX_QUEUE:
            return
        self._queued.add(key)
        self._queue.put_nowait(key)

    def process_json(self, data: Any, cfg: dict, sink: dict | None = None) -> int:
        """Rewrite image tags in parsed JSON, in place. Returns tags changed.
        ``sink`` parks the due checks instead of queueing them (see ``resolve``)."""
        image_types = cfg["image_types"]
        item_types = set(cfg["item_types"])
        changed = 0
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(c for c in node if isinstance(c, (dict, list)))
                continue
            if not isinstance(node, dict):
                continue
            item_id = node.get("Id")
            if isinstance(item_id, str) and item_id:
                tags = node.get("ImageTags")
                item_type = node.get("Type")
                if isinstance(tags, dict) and (item_type is None or item_type in item_types):
                    changed += self._rewrite_own(node, item_id, tags, image_types, sink)
                changed += self._rewrite_parent_refs(node)
            stack.extend(c for c in node.values() if isinstance(c, (dict, list)))
        if changed:
            self.stats["tags_rewritten"] += changed
        return changed

    def _rewrite_own(self, node: dict, item_id: str, tags: dict, image_types: list,
                     sink: dict | None = None) -> int:
        n = 0
        for img_type in image_types:
            if img_type == "Backdrop":
                # Remux reports the backdrop twice: ImageTags.Backdrop and
                # BackdropImageTags[0]. Both must carry the same emitted tag.
                bd = node.get("BackdropImageTags")
                bd0 = bd[0] if isinstance(bd, list) and bd and isinstance(bd[0], str) else None
                tag_bd = tags.get("Backdrop") if isinstance(tags.get("Backdrop"), str) else None
                orig = tag_bd or bd0
                if not orig:
                    continue
                new = self.resolve(item_id, "Backdrop", orig, sink)
                if new != orig:
                    if tag_bd == orig:
                        tags["Backdrop"] = new
                        n += 1
                    if bd0 == orig:
                        bd[0] = new
                        n += 1
                continue
            orig = tags.get(img_type)
            if isinstance(orig, str) and orig:
                new = self.resolve(item_id, img_type, orig, sink)
                if new != orig:
                    tags[img_type] = new
                    n += 1
        return n

    def _rewrite_parent_refs(self, node: dict) -> int:
        n = 0
        for tag_field, id_field, types in PARENT_TAG_FIELDS:
            orig, pid = node.get(tag_field), node.get(id_field)
            if isinstance(orig, str) and orig and isinstance(pid, str) and pid:
                new = self._known(pid, types, orig)
                if new and new != orig:
                    node[tag_field] = new
                    n += 1
        bds, pid = node.get("ParentBackdropImageTags"), node.get("ParentBackdropItemId")
        if isinstance(bds, list) and bds and isinstance(bds[0], str) and isinstance(pid, str):
            new = self._known(pid, ("Backdrop",), bds[0])
            if new and new != bds[0]:
                bds[0] = new
                n += 1
        return n

    # ---- background checks -----------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        assert self._queue is not None
        settings = self.ctx.settings
        while True:
            key = await self._queue.get()
            try:
                cfg = settings.get(self.name)
                if not cfg["enabled"]:
                    self._queued.discard(key)
                    continue
                while self._inflight >= cfg["probe_concurrency"]:
                    await asyncio.sleep(0.05)
                    cfg = settings.get(self.name)
                self._inflight += 1
                try:
                    self._spawn(self._probe_task(key))
                except BaseException:
                    self._inflight -= 1
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let one bad key kill the loop: checks would stop forever.
                self._queued.discard(key)
                log.exception("poster dispatcher error for %s", key)

    async def _probe_task(self, key: Key) -> None:
        try:
            cfg = self.ctx.settings.get(self.name)
            rec = self.records.get(key)
            if rec is None:  # purged meanwhile
                return
            try:
                fp, _size, _ctype = await self.fetch(
                    key[0], key[1],
                    timeout=cfg["probe_timeout_seconds"],
                    max_bytes=cfg["max_image_mb"] * 1024 * 1024,
                )
            except ProbeError as exc:
                self._on_failure(key, rec, str(exc), cfg)
            else:
                self._on_success(key, rec, fp, cfg)
        except Exception:
            log.exception("poster check crashed for %s", key)
        finally:
            self._inflight -= 1
            self._queued.discard(key)

    async def fetch(
        self, item_id: str, img_type: str, *, timeout: float, max_bytes: int,
    ) -> tuple[str, int, str]:
        """Fetch an image from Remux and hash it. Returns (sha256, size, content_type)."""
        url = f"{self.ctx.config.remux_url}/Items/{quote(item_id, safe='')}/Images/{img_type}"
        h = hashlib.sha256()
        total = 0
        ctype = ""
        try:
            async with asyncio.timeout(timeout):
                async with self.ctx.client.stream(
                    "GET", url, headers={"accept-encoding": "identity"},
                    timeout=httpx.Timeout(timeout),
                ) as resp:
                    if resp.status_code != 200:
                        raise ProbeError(f"Remux answered HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "")
                    if not ctype.lower().startswith("image/"):
                        raise ProbeError(f"not an image ({ctype or 'no content-type'})")
                    async for chunk in resp.aiter_raw():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProbeError(f"image larger than {max_bytes // 1048576} MB")
                        h.update(chunk)
        except ProbeError:
            raise
        except TimeoutError as exc:
            raise ProbeError("timed out") from exc
        except httpx.HTTPError as exc:
            raise ProbeError(f"{type(exc).__name__}: {exc}") from exc
        if total == 0:
            raise ProbeError("empty image")
        return h.hexdigest(), total, ctype

    def _on_success(self, key: Key, rec: Rec, fp: str, cfg: dict) -> None:
        now = time.time()
        self.stats["checks_ok"] += 1
        rec.fail_count = 0
        rec.last_checked = now
        rec.next_check = now + cfg["recheck_hours"] * 3600 * (1 + random.uniform(0, 0.1))
        if rec.fp is None:
            rec.fp = fp  # silent baseline
        elif rec.fp != fp:
            left = rec.changed_at + cfg["min_change_interval_hours"] * 3600 - now
            if left > 0:
                # Keep the old fingerprint so the change is picked up once the
                # cooldown is over (unless the picture flips back meanwhile).
                self.stats["changes_held_by_cooldown"] += 1
                self.ctx.events.info(
                    "POSTER",
                    f"{key[1]} of {key[0][:8]} changed but is cooling down "
                    f"({left / 3600:.1f} h left); tag unchanged",
                )
            else:
                old = make_tag(rec.orig_tag, rec.version, self.epoch)
                rec.fp = fp
                rec.version += 1
                rec.changed_at = now
                self.stats["changes_detected"] += 1
                new = make_tag(rec.orig_tag, rec.version, self.epoch)
                self.ctx.events.info(
                    "POSTER",
                    f"{key[1]} of {key[0][:8]} changed: tag {old[:8]} -> {new[:8]} (v{rec.version})",
                )
        self._dirty.add(key)

    def _on_failure(self, key: Key, rec: Rec, reason: str, cfg: dict) -> None:
        rec.fail_count += 1
        self.stats["checks_failed"] += 1
        delay = min(cfg["recheck_hours"] * 3600, 300 * 2 ** min(rec.fail_count - 1, 12))
        rec.next_check = time.time() + delay
        if rec.fail_count <= 3:
            self.ctx.events.warn(
                "POSTER",
                f"check failed for {key[1]} of {key[0][:8]}: {reason} "
                f"(retry in {int(delay // 60)} min)",
            )
        self._dirty.add(key)

    # ---- admin actions ----------------------------------------------------------------------
    async def force_refresh(self) -> int:
        assert self._store
        self.epoch += 1
        await asyncio.to_thread(self._store.set_epoch, self.epoch)
        self.ctx.events.warn(
            "POSTER", f"FORCE REFRESH: every tracked image now has a new tag (epoch {self.epoch})"
        )
        return self.epoch

    async def purge(self) -> int:
        assert self._store and self._queue is not None
        n = len(self.records)
        self.records = {}
        self._dirty.clear()
        self.epoch = 0
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queued.clear()
        await asyncio.to_thread(self._store.clear)
        self.ctx.events.warn("POSTER", f"PURGE: forgot {n} tracked images; tags revert to Remux's own")
        return n

    def inspect(self, item_id: str) -> list[dict[str, Any]]:
        item_id = item_id.lower()
        return [
            {
                "image_type": typ,
                "remux_tag": r.orig_tag,
                "emitted_tag": make_tag(r.orig_tag, r.version, self.epoch),
                "version": r.version,
                "fingerprint": r.fp[:16] if r.fp else None,
                "last_checked": r.last_checked or None,
                "next_check": r.next_check or None,
                "changed_at": r.changed_at or None,
                "failures": r.fail_count,
            }
            for (iid, typ), r in self.records.items()
            if iid.lower() == item_id
        ]

    async def probe_now(self, item_id: str, img_type: str) -> dict[str, Any]:
        """Diagnostic: fetch one image right now. Does not alter tracked state."""
        cfg = self.ctx.settings.get(self.name)
        try:
            fp, size, ctype = await self.fetch(
                item_id, img_type,
                timeout=cfg["probe_timeout_seconds"],
                max_bytes=cfg["max_image_mb"] * 1024 * 1024,
            )
        except ProbeError as exc:
            return {"ok": False, "error": str(exc)}
        rec = next(
            (r for (iid, typ), r in self.records.items()
             if typ == img_type and iid.lower() == item_id.lower()),
            None,
        )
        return {
            "ok": True,
            "sha256": fp[:16],
            "bytes": size,
            "content_type": ctype,
            "matches_recorded": (rec.fp == fp) if rec and rec.fp else None,
        }

    def public_stats(self) -> dict[str, Any]:
        return {
            **self.stats,
            "tracked_images": len(self.records),
            "waiting_checks": len(self._queued),
            "checks_running": self._inflight,
            "epoch": self.epoch,
            "images_with_new_tag": sum(
                1 for r in self.records.values() if r.version or self.epoch
            ),
        }
