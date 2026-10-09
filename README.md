# Refused

One container that sits between Infuse and Remux and bundles two tools, each
switched on or off from a web panel at `/refused`:

| Module | What it does |
|---|---|
| **STREAM_STRIP** | Removes resolution, bitrate, codec, HDR and audio-layout fields from `MediaStreams`, so Infuse's auto-picker has nothing to rank and takes the first version Remux lists. (This is the old `remux-stream-strip-proxy`, now configurable.) |
| **POSTER_REFRESH** | Makes Infuse notice when artwork really changed, without piling up duplicate cached copies. Images are never buffered or modified. |

**SEE_ALL** makes "see all" on a home row show the whole catalog instead of
50. It only rewrites the `limit=50` on `/Items/Latest`; see the LOG tab after
tapping "see all" to check how many items Remux sent. Set MAX_ITEMS to cap it.

**SEARCH_CLEAN** leaves cast and crew out of Infuse search results (titles only).
It only answers `/Persons` requests that carry a search term.

There is also a **DIAGNOSTICS** switch (off by default): a read-only logger
that shows in the LOG tab what Infuse asks Remux for (`Limit`, `StartIndex`)
and how many items come back. It changes nothing.

With everything off, Refused is a transparent proxy. The upcoming-catalog worker is
not part of this container; it keeps running as before.

## Setup

1. `cp .env.example .env` and set `REFUSED_USERNAME`, `REFUSED_PASSWORD` (it
   refuses to start with the placeholder) and `REMUX_URL`.
2. Add the service from `docker-compose.snippet.yml` and
   `docker compose up -d --build refused`.
3. Open `http://<host>:8099/refused`, sign in, switch modules on.
4. In Infuse, point the Remux server at `<host>:8099` instead of Remux.
   Stop the old stream-strip container, or it competes for the same job.

Run a single instance. State lives in memory and `/data` (settings, poster
database, session key); mount `/data` so it survives rebuilds.

## POSTER_REFRESH: how it works

Remux's image tag is the image row's ID, and it stays the same when an addon
serves a different picture. Infuse only re-downloads when the tag changes, so
swapped posters never show up.

Refused watches the posters Infuse is shown, re-fetches each one in the
background on a schedule, and hashes the bytes. When the hash changes it
changes that item's tag. The tag is a pure function of Remux's tag plus a
change counter, so:

- unchanged artwork keeps the same tag forever (no duplicate cache entries);
- the first sighting is recorded silently, so enabling the module does not
  make Infuse re-download your library;
- restarts do not flip tags (state is persisted);
- a failed check never changes a tag;
- a **cooldown** stops posters that bake in a changing rating from creating a
  new cache copy every time the score moves.

### Read this before relying on it

There is no official documentation on how Infuse treats a changed image tag.
Forum reports suggest Infuse caches server metadata hard and may not re-pull
artwork when a tag changes. There is also no way for a sidecar to clear
Infuse's cache. Test before trusting it:

1. In POSTER_REFRESH turn on **LOG_IMAGE_REQUESTS**.
2. Press **FORCE_REFRESH_ALL** (gives every tracked image a new tag once).
3. Browse the library in Infuse and watch the LOG tab. If Infuse requests
   images again with the new tag, the approach works. If it does not, Infuse
   is ignoring tags and refreshing needs Infuse-side action (Settings →
   Metadata & Artwork).
4. Watch Infuse's metadata size (Settings → General) across a few cycles to
   see whether old copies ever drop out.

## Security

Credentials come from `.env`. Sessions are signed, HttpOnly, SameSite=Strict
cookies bound to the current password (changing it signs everyone out); writes
need a CSRF token; five failed sign-ins lock that address for 15 minutes; the
page ships a strict Content-Security-Policy. It speaks plain HTTP, so keep it
on your LAN or put TLS in front (and set `COOKIE_SECURE=true`).

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
- JSON bodies over 32 MB (`MAX_JSON_MB`) are streamed through unmodified.

## Running

One process only (the Dockerfile does this): settings, sessions and poster
state live in memory. The container runs as root so it can write the mounted
`/data` folder without permission setup.

## Development

```
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```
