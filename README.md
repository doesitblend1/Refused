# Refused

A small proxy that sits between Infuse and Remux and changes a few things Infuse
sees. It is built from separate modules, each switched on or off (and tuned)
from a web panel at `/refused`. With every module off it is a transparent proxy
and Infuse sees no difference.

| Module | What it does |
|---|---|
| **STREAM_STRIP** | Removes resolution, bitrate, codec, HDR and audio-layout fields from `MediaStreams`, so Infuse's auto-picker has nothing to rank and takes the first version Remux lists. (The old `remux-stream-strip-proxy`, now configurable.) |
| **POSTER_REFRESH** | Makes Infuse notice when artwork really changed, without piling up duplicate cached copies. Images are never buffered or modified on their way to Infuse. |
| **SEE_ALL** | Makes "see all" on a home row show the whole catalog instead of 50. |
| **SEARCH_CLEAN** | Leaves cast and crew out of Infuse search results (titles only). |
| **DIAGNOSTICS** | Read-only logger: shows in the LOG tab what Infuse asks Remux for and how many items come back. Changes nothing. |

The upcoming-catalog worker is not part of this container; it keeps running as
before.

## Setup

1. `cp .env.example .env` and set `REFUSED_USERNAME`, `REFUSED_PASSWORD` (it
   refuses to start with the placeholder) and `REMUX_URL`.
2. Add the service from `docker-compose.snippet.yml` and
   `docker compose up -d --build refused`. The panel header shows the running
   version; after an update it should match the newest entry in `CHANGELOG.md`.
3. Open `http://<host>:8099/refused`, sign in, switch modules on.
4. In Infuse, point the Remux server at `<host>:8099` instead of Remux.
   Stop the old stream-strip container, or it competes for the same job.

Run a single instance. State lives in memory and `/data` (settings, poster
database, session key); mount `/data` so it survives rebuilds.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `REFUSED_USERNAME`, `REFUSED_PASSWORD` | required | Panel login. The password cannot be `CHANGE_ME`. |
| `REMUX_URL` | `http://remux:3000` | Where Remux is reachable from inside the container. |
| `STREAM_STRIP_DEFAULT`, `POSTER_REFRESH_DEFAULT`, `SEE_ALL_DEFAULT`, `SEARCH_CLEAN_DEFAULT`, `DIAGNOSTICS_DEFAULT` | `false` | Starting state of each module on the very first run only. After that the panel's switches win. |
| `REQUEST_TIMEOUT` | `30` | Seconds before an ordinary upstream request gives up. |
| `SESSION_HOURS` | `12` | Panel sign-in lifetime. |
| `COOKIE_SECURE` | `false` | Set `true` only behind HTTPS. |
| `MAX_JSON_MB` | `32` | JSON bodies larger than this are streamed through unmodified. |
| `DATA_DIR`, `LISTEN_PORT` | `/data`, `8080` | Set by the Dockerfile; change only if you know why. |
| `LOG_LEVEL` | `INFO` | Container log level. |

Everything else (the module options below) lives in the panel and is saved to
`/data/settings.json`.

## STREAM_STRIP

Removes the listed fields from every `MediaStreams` entry Remux returns.
Names, titles, languages and playback locators are never touched.

- **SCOPE**: `All JSON responses` (PlaybackInfo, item details and episode
  lists; Infuse's manual version picker also loses its technical badges) or
  `PlaybackInfo only` (details pages keep their badges, but auto-pick may still
  use quality if Infuse decides from item details).
- **STRIPPED_FIELDS**: one field name per line, case-insensitive. Add a name if
  Infuse still reacts to something; remove one to let Infuse see it again.

## POSTER_REFRESH

### How it works

Remux's image tag is the image row's ID, and it stays the same when an addon
serves a different picture. Infuse only re-downloads when the tag changes, so
swapped posters never show up.

Refused notices which posters Infuse is shown, checks each one in the
background, and hashes the bytes (only the hash is stored, never the image).
When the hash changes it changes that item's tag. The tag is a pure function of
Remux's tag plus a change counter, so:

- unchanged artwork keeps the same tag forever (no duplicate cache entries);
- the first sighting is recorded silently, so enabling the module does not make
  Infuse re-download your library;
- restarts do not flip tags (state is persisted);
- a failed check never changes a tag;
- a **cooldown** stops posters that bake in a changing rating from creating a
  new cache copy every time the score moves.

Only items Infuse is actually sent get checked. A check is queued the first
time an item is seen and again once RECHECK_EVERY has passed. In a long
"see all" (SEE_ALL), checks follow what has been delivered: the first part right
away, each later page when Infuse asks for it as you scroll. Remux fetches
every poster live from its source on each request and does not rate-limit
image fetches, so nothing here paces the checks; the only cap is
PARALLEL_CHECKS.

### Options

| Option | Default | Meaning |
|---|---|---|
| IMAGE_TYPES | Primary | Which artwork to watch (Primary, Backdrop, Logo, Thumb). Each extra type adds checks and one more thing that can be cached twice in Infuse. |
| ITEM_TYPES | Movie, Series, Season, BoxSet | Which kinds of items to watch. Episodes are off by default: there are many and their thumbnails rarely change. |
| RECHECK_EVERY | 24 hours | How often each image is re-fetched and compared. |
| CHANGE_COOLDOWN | 48 hours | After a tag changes, ignore further changes to that item for this long. `0` disables it. |
| PARALLEL_CHECKS | 16 | How many checks may run at once (1-64). Not a rate limit: it only stops a burst from starting hundreds of downloads together. Lower it if your poster addon struggles. |
| CHECK_TIMEOUT | 20 seconds | Give up on one image fetch after this long; it is retried later with a growing delay. |
| MAX_IMAGE_SIZE | 12 MB | Larger images are skipped and never re-tagged. |
| LOG_IMAGE_REQUESTS | off | Write every image request Infuse makes to the LOG tab, with the tag it asked for. Noisy; for testing. |

The panel also has **FORCE_REFRESH_ALL** (gives every tracked image a new tag
once) and **PURGE_STATE** (forgets everything and returns to Remux's own tags).

### Read this before relying on it

There is no official documentation on how Infuse treats a changed image tag.
Forum reports suggest Infuse caches server metadata hard and may not re-pull
artwork when a tag changes. There is also no way for a sidecar to clear Infuse's
cache. Test before trusting it:

1. In POSTER_REFRESH turn on **LOG_IMAGE_REQUESTS**.
2. Press **FORCE_REFRESH_ALL**.
3. Browse the library in Infuse and watch the LOG tab. If Infuse requests
   images again with the new tag, the approach works. If it does not, Infuse is
   ignoring tags and refreshing needs Infuse-side action (Settings → Metadata &
   Artwork).
4. Watch Infuse's metadata size (Settings → General) across a few cycles to see
   whether old copies ever drop out.

## SEE_ALL

Infuse's home rows ask Remux for 20 items and "see all" asks for 50. The
"latest" lists have no total, so Infuse stops there. SEE_ALL rewrites only the
`/Items/Latest` request that has `limit=50` so Remux sends the whole catalog.
Home rows, favorites, search and everything else are untouched.

Infuse treats the length of the first response as the list's total and stacks
every page it asks for afterwards, so the list is delivered in two parts: the
first response carries about half, and the follow-up pages you scroll into
carry the rest, in order, with no repeats and no empty pages. The split is kept
per catalog until the next "see all" for it (the last 20 are remembered). A
catalog of exactly 51 items has no clean split and is sent whole.

- **MAX_ITEMS** (default 0 = everything): cap very large catalogs; bigger lists
  load more slowly.
- **TRIGGER_SIZE** (default 50): only requests for exactly this many items are
  expanded. Use DIAGNOSTICS to confirm what your Infuse sends.

How many items come back is up to Remux; the LOG tab shows the count for each
"see all".

## SEARCH_CLEAN

Infuse runs two requests for every search: titles (`/Items?searchTerm=`) and
people (`/Persons?searchTerm=`). While enabled, the people request is answered
with an empty list, so search shows titles only. Requests without a search term
(browsing, a show's own cast, tapping an actor from a cast list) are not
touched.

- **HIDE_FROM_SEARCH** (default People): what to leave out. Only people
  searches are known to be sent separately by Infuse.

## DIAGNOSTICS

Logs list-style responses to the LOG tab with the `Limit` and `StartIndex`
Infuse asked for and how many items came back out of how many exist. Bodies are
not stored and tokens in URLs are masked. JSON responses are buffered while it
is on, so turn it off when you are done.

- **SCOPE**: `List requests only` (default) or `Every JSON request` (path and
  status of every JSON response; noisy).

## The panel

One tab per module for its options, a STATUS tab (module states and the
UPSTREAM panel: Remux's address, reachability and `REMUX_VERSION`, shown when
Remux reports one) and the LOG tab. Each option has a description next to it.
Changes apply immediately and are saved to `/data/settings.json`.

## Security

Credentials come from `.env`. Sessions are signed, HttpOnly, SameSite=Strict
cookies bound to the current password (changing it signs everyone out); writes
need a CSRF token; five failed sign-ins lock that address for 15 minutes; the
page ships a strict Content-Security-Policy. It speaks plain HTTP, so keep it on
your LAN or put TLS in front (and set `COOKIE_SECURE=true`).

## If Infuse was already set up through a proxy

Remux tells clients its own address in `/System/Info/Public` (`LocalAddress`),
built from the `X-Forwarded-Host` / `Host` header, and Jellyfin clients save it.
The old `remux-stream-strip-proxy` dropped the client's Host, so Remux
advertised *its own* address. Infuse may have quietly switched to talking to
Remux directly, skipping the proxy. Refused forwards `X-Forwarded-Host`, so
Remux now advertises Refused. If you used the old proxy, remove and re-add the
server in Infuse pointing at Refused so it picks up the right address.

## Limits worth knowing

- A `Location` redirect is only rewritten when it points at `REMUX_URL` itself.
- Poster records are kept for every image ever seen; nothing prunes items
  deleted from Remux (a few hundred bytes each).
- Parent artwork repeated on children (`SeriesPrimaryImageTag` etc.) is only
  re-tagged once the parent item itself has been seen and tracked.
- WebSocket upgrades are not proxied.
- JSON bodies over `MAX_JSON_MB` are streamed through unmodified.
- Not verified in Infuse yet: SEARCH_CLEAN hiding people, and whether a changed
  poster tag makes Infuse re-download the image.

## Running

One process only (the Dockerfile does this): settings, sessions and poster
state live in memory. The container runs as root so it can write the mounted
`/data` folder without permission setup.

## Development

```
pip install -r requirements.txt
python -m unittest tests.test_refused
```

`CHANGELOG.md` lists every change by version.
