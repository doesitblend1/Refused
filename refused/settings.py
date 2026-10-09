"""Behaviour settings: schema (also drives the admin UI), validation, storage.

The schema below is the single source of truth. The admin panel renders its
forms from it, so every label and description a user sees lives here.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("refused.settings")

STRIP_DEFAULT_FIELDS = [
    "Width", "Height", "BitRate", "BitDepth", "Codec", "CodecTag", "Profile",
    "Level", "RefFrames", "AverageFrameRate", "RealFrameRate", "PixelFormat",
    "ColorSpace", "ColorTransfer", "ColorPrimaries", "ColorRange",
    "ChannelLayout", "Channels", "SampleRate", "AspectRatio", "IsAnamorphic",
    "IsInterlaced", "VideoRange", "VideoRangeType", "VideoDoViTitle",
    "NalLengthSize",
]

# What "show everything" asks Remux for in SEE_ALL: far above any real catalog,
# far below anything a server might reject or pre-allocate for.
SEE_ALL_EVERYTHING = 10000
SETTINGS_FILE_VERSION = 2  # 2: PARALLEL_CHECKS default 3 -> 16, CHECK_RATE removed

POSTER_IMAGE_TYPES = ["Primary", "Backdrop", "Logo", "Thumb"]
POSTER_ITEM_TYPES = ["Movie", "Series", "Season", "Episode", "BoxSet"]

SCHEMA: dict[str, dict[str, Any]] = {
    "stream_strip": {
        "label": "STREAM_STRIP",
        "tagline": "Hide quality details so Infuse stops ranking versions",
        "description": (
            "Removes resolution, bitrate, codec, HDR and audio-layout fields from "
            "every MediaStreams entry Remux returns. Infuse's auto-picker then has "
            "nothing to score and falls back to the first version Remux lists. "
            "Names, titles, languages and playback locators are never touched."
        ),
        "fields": [
            {
                "key": "scope",
                "type": "select",
                "label": "SCOPE",
                "default": "all",
                "options": [
                    {
                        "value": "all",
                        "label": "All JSON responses",
                        "help": (
                            "Strip everywhere Remux sends stream info: PlaybackInfo, "
                            "item details and episode lists. Infuse's manual version "
                            "picker also loses its technical badges."
                        ),
                    },
                    {
                        "value": "playback",
                        "label": "PlaybackInfo only",
                        "help": (
                            "Strip only on PlaybackInfo responses. Details pages keep "
                            "their badges, but if Infuse decides from item details "
                            "instead, auto-pick may still use quality."
                        ),
                    },
                ],
                "help": "Which responses get cleaned.",
            },
            {
                "key": "fields",
                "type": "lines",
                "label": "STRIPPED_FIELDS",
                "default": STRIP_DEFAULT_FIELDS,
                "help": (
                    "One field name per line, case-insensitive, matched only inside "
                    "MediaStreams entries. Add a name if Infuse still reacts to "
                    "something; remove one to let Infuse see that field again."
                ),
            },
        ],
    },
    "poster_refresh": {
        "label": "POSTER_REFRESH",
        "tagline": "Make Infuse notice when artwork really changes, without piling up copies",
        "description": (
            "Infuse keeps artwork it has already downloaded and only fetches again "
            "when an item's image tag changes. Remux's tag is a fixed ID, so a "
            "poster swapped by a ratings or 'better posters' addon never looks new. "
            "Refused hashes the actual image bytes in the background and changes an "
            "item's tag only when the picture itself changed. The first time an "
            "image is seen it is recorded silently with its original tag, so "
            "turning this on does not make Infuse re-download your library."
        ),
        "fields": [
            {
                "key": "image_types",
                "type": "multiselect",
                "label": "IMAGE_TYPES",
                "default": ["Primary"],
                "options": [{"value": v, "label": v} for v in POSTER_IMAGE_TYPES],
                "help": (
                    "Which artwork to watch. Primary is the poster. Each extra type "
                    "adds background checks and one more thing that can be cached "
                    "twice in Infuse."
                ),
            },
            {
                "key": "item_types",
                "type": "multiselect",
                "label": "ITEM_TYPES",
                "default": ["Movie", "Series", "Season", "BoxSet"],
                "options": [{"value": v, "label": v} for v in POSTER_ITEM_TYPES],
                "help": (
                    "Which kinds of items to watch. Episodes are off by default: "
                    "there are many of them and their thumbnails rarely change."
                ),
            },
            {
                "key": "recheck_hours",
                "type": "int",
                "label": "RECHECK_EVERY",
                "unit": "hours",
                "default": 24,
                "min": 1,
                "max": 720,
                "help": (
                    "How often each image is re-fetched and compared. Lower catches "
                    "changes sooner but asks Remux, and the addon behind it, to "
                    "generate more posters."
                ),
            },
            {
                "key": "min_change_interval_hours",
                "type": "int",
                "label": "CHANGE_COOLDOWN",
                "unit": "hours",
                "default": 48,
                "min": 0,
                "max": 720,
                "help": (
                    "After an item's tag changes, ignore further changes to it for "
                    "this long. Posters that bake in a rating can change a pixel "
                    "every time the score moves; each tag change is another copy in "
                    "Infuse's cache. 0 disables the cooldown."
                ),
            },
            {
                "key": "probe_concurrency",
                "type": "int",
                "label": "PARALLEL_CHECKS",
                "unit": "at once",
                "default": 16,
                "min": 1,
                "max": 64,
                "help": (
                    "How many image checks may run at the same time. This is not a "
                    "rate limit: it only stops a burst (a big list opening) from "
                    "starting hundreds of downloads at once. Remux fetches each "
                    "poster live from its source, so lower this if your poster "
                    "addon or server struggles during a burst."
                ),
            },
            {
                "key": "probe_timeout_seconds",
                "type": "int",
                "label": "CHECK_TIMEOUT",
                "unit": "seconds",
                "default": 20,
                "min": 3,
                "max": 120,
                "help": (
                    "Give up on a single image fetch after this long. A failed check "
                    "never changes a tag; it is retried later with a growing delay."
                ),
            },
            {
                "key": "max_image_mb",
                "type": "int",
                "label": "MAX_IMAGE_SIZE",
                "unit": "MB",
                "default": 12,
                "min": 1,
                "max": 100,
                "help": "Images bigger than this are skipped (and never re-tagged).",
            },
            {
                "key": "log_image_requests",
                "type": "bool",
                "label": "LOG_IMAGE_REQUESTS",
                "default": False,
                "help": (
                    "Write every image request Infuse makes to the LOG tab, with the "
                    "tag it asked for. Turn on while testing to confirm Infuse "
                    "re-fetches after a tag change; turn off afterwards, it is noisy."
                ),
            },
        ],
    },
    "see_all": {
        "label": "SEE_ALL",
        "tagline": "Make 'see all' on a home row show the whole catalog, not just 50",
        "description": (
            "Infuse's home rows ask Remux for 20 items, and tapping 'see all' asks "
            "for 50. The 'latest' lists have no total count, so Infuse stops there. "
            "While enabled, Refused swaps that 50 for a much bigger number on the "
            "'latest items' request only, so Remux sends the whole catalog. Home "
            "rows themselves, favorites, search and everything else are left alone. "
            "Infuse stacks every page it receives, so the list is delivered in "
            "two parts: the first response carries about half, and the pages "
            "Infuse then asks for as you scroll carry the rest, in order, with no "
            "repeats. How many items exist is up to Remux; the LOG tab shows the "
            "number it returned for each tap."
        ),
        "fields": [
            {
                "key": "max_items",
                "type": "int",
                "label": "MAX_ITEMS",
                "unit": "items",
                "default": 0,
                "min": 0,
                "max": 100000,
                "help": (
                    "How many items 'see all' may show. 0 means everything (Refused "
                    f"asks for {SEE_ALL_EVERYTHING:,}). Enter a number such as 300 to "
                    "cap very large catalogs: bigger lists load more slowly. A number "
                    "at or below the trigger size does nothing."
                ),
            },
            {
                "key": "trigger_limit",
                "type": "int",
                "label": "TRIGGER_SIZE",
                "unit": "items",
                "default": 50,
                "min": 1,
                "max": 1000,
                "help": (
                    "Only requests for exactly this many items are expanded. 50 is "
                    "what Infuse sends for 'see all' (use the DIAGNOSTICS log to "
                    "check). The 20-item home rows never match, so they stay quick."
                ),
            },
        ],
    },
    "search_clean": {
        "label": "SEARCH_CLEAN",
        "tagline": "Keep cast and crew out of Infuse search results",
        "description": (
            "Infuse runs two requests for every search: one for titles and one for "
            "people (actors, directors). While enabled, Refused answers the people "
            "request with an empty list, but only when it carries a search term, "
            "so search shows titles only. Browsing catalogs, opening a show, a "
            "show's own cast list and tapping an actor from a cast list are not "
            "touched."
        ),
        "fields": [
            {
                "key": "hide",
                "type": "multiselect",
                "label": "HIDE_FROM_SEARCH",
                "default": ["People"],
                "options": [{"value": "People", "label": "People (cast & crew)"}],
                "help": (
                    "What to leave out of search results. Only people searches are "
                    "known to be sent separately by Infuse; the DIAGNOSTICS log "
                    "(scope: every JSON request) shows what else a search asks for."
                ),
            },
        ],
    },
    "diagnostics": {
        "label": "DIAGNOSTICS",
        "tagline": "See what Infuse asks Remux for (list sizes, limits, paging)",
        "description": (
            "A read-only request logger. While enabled, every list-style response "
            "that passes through is written to the LOG tab with the Limit and "
            "StartIndex Infuse asked for and how many items Remux sent back out of "
            "how many exist. Use it to find out what Infuse requests when you open "
            "a catalog or tap 'see all'. Nothing is changed, response bodies are "
            "not stored, and login tokens in URLs are masked. Turn it off when "
            "you are done; JSON responses are buffered while it is on."
        ),
        "fields": [
            {
                "key": "scope",
                "type": "select",
                "label": "SCOPE",
                "default": "lists",
                "options": [
                    {
                        "value": "lists",
                        "label": "List requests only",
                        "help": (
                            "Log responses that carry an Items list or were asked "
                            "with Limit/StartIndex. This is what you want for the "
                            "'see all' question."
                        ),
                    },
                    {
                        "value": "all_json",
                        "label": "Every JSON request",
                        "help": (
                            "Log the path and status of every JSON response. Noisy, "
                            "but useful if the request you are after is not a "
                            "plain item list."
                        ),
                    },
                ],
                "help": "Which requests get a line in the LOG tab.",
            },
        ],
    },
}


class ValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _field_map(module: str) -> dict[str, dict]:
    return {f["key"]: f for f in SCHEMA[module]["fields"]}


def default_settings(module: str, enabled_default: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"enabled": enabled_default}
    for f in SCHEMA[module]["fields"]:
        out[f["key"]] = copy.deepcopy(f["default"])
    return out


def coerce(field: dict, value: Any) -> Any:
    """Validate and normalise one value; raise ValueError with a readable message."""
    label = field["label"]
    t = field["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{label} must be true or false")
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a number")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{label} must be a whole number")
        n = int(value)
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and n < lo:
            raise ValueError(f"{label} must be at least {lo}")
        if hi is not None and n > hi:
            raise ValueError(f"{label} must be at most {hi}")
        return n
    if t == "select":
        allowed = [o["value"] for o in field["options"]]
        if value not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        return value
    if t == "multiselect":
        allowed = [o["value"] for o in field["options"]]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(f"{label} must be a list")
        bad = [v for v in value if v not in allowed]
        if bad:
            raise ValueError(f"{label} has unknown value(s): {', '.join(bad)}")
        # Keep schema order, drop duplicates.
        return [v for v in allowed if v in value]
    if t == "lines":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(f"{label} must be a list of text lines")
        seen, out = set(), []
        for raw in value:
            item = raw.strip()
            if not item:
                continue
            if len(item) > 128 or any(c in item for c in '"\\\0'):
                raise ValueError(f"{label} has an invalid entry: {item[:40]!r}")
            if item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
        if len(out) > 500:
            raise ValueError(f"{label} has too many entries (max 500)")
        return out
    raise ValueError(f"{label}: unsupported field type {t}")


def public_schema() -> dict[str, Any]:
    """Schema as sent to the browser (it is already JSON-safe)."""
    return copy.deepcopy(SCHEMA)


class SettingsStore:
    """Thread-safe, file-backed settings. Readers get immutable-by-convention dicts."""

    def __init__(self, path: Path, enabled_defaults: dict[str, bool] | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._enabled_defaults = enabled_defaults or {}
        self._data: dict[str, dict[str, Any]] = {
            m: default_settings(m, self._enabled_defaults.get(m, False)) for m in SCHEMA
        }
        self._load()

    # ---- reading -------------------------------------------------------
    def get(self, module: str) -> dict[str, Any]:
        """Current settings for a module. Treat the result as read-only."""
        return self._data[module]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._data)

    # ---- writing -------------------------------------------------------
    def update(self, module: str, patch: dict[str, Any]) -> dict[str, Any]:
        if module not in SCHEMA:
            raise ValidationError([f"unknown module {module!r}"])
        if not isinstance(patch, dict):
            raise ValidationError(["settings must be an object"])
        fields = _field_map(module)
        errors: list[str] = []
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key == "enabled":
                if isinstance(value, bool):
                    clean[key] = value
                else:
                    errors.append("enabled must be true or false")
            elif key in fields:
                try:
                    clean[key] = coerce(fields[key], value)
                except ValueError as exc:
                    errors.append(str(exc))
            else:
                errors.append(f"unknown setting {key!r}")
        if errors:
            raise ValidationError(errors)
        with self._lock:
            new = {**self._data[module], **clean}
            self._data[module] = new  # swap, never mutate in place
            self._save()
        return new

    def reset(self, module: str) -> dict[str, Any]:
        if module not in SCHEMA:
            raise ValidationError([f"unknown module {module!r}"])
        with self._lock:
            keep_enabled = self._data[module]["enabled"]
            self._data[module] = default_settings(module, keep_enabled)
            self._save()
        return self._data[module]

    # ---- persistence ---------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); using defaults", self._path, exc)
            return
        if not isinstance(raw, dict):
            return
        old_file = raw.get("_version", 1) < SETTINGS_FILE_VERSION
        for module, saved in raw.items():
            if module not in SCHEMA or not isinstance(saved, dict):
                continue
            if old_file and module == "poster_refresh" and saved.get("probe_concurrency") == 3:
                saved = {**saved, "probe_concurrency": 16}  # 3 was the old default
            fields = _field_map(module)
            merged = self._data[module]
            if isinstance(saved.get("enabled"), bool):
                merged["enabled"] = saved["enabled"]
            for key, value in saved.items():
                if key in fields:
                    try:
                        merged[key] = coerce(fields[key], value)
                    except ValueError as exc:
                        log.warning("ignoring saved %s.%s: %s", module, key, exc)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self._data, "_version": SETTINGS_FILE_VERSION},
                                      indent=2, sort_keys=True))
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("could not write %s: %s", self._path, exc)
