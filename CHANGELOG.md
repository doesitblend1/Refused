# Changelog

## 1.8.0

- POSTER_REFRESH checks now follow what Infuse is actually sent. A long
  "see all" used to queue a check for every item in the catalog the moment it
  opened, although Infuse is only given the first part and the rest as you
  scroll. Now the held-back items are checked when their page is sent. (Tags
  are still rewritten for the whole list up front.)
- Removed CHECK_RATE. Remux does not rate-limit image fetches, but the poster
  addon is yours and the cap on simultaneous checks is what protects it.
- PARALLEL_CHECKS now defaults to 16 (range 1-64). A saved value of 3, the
  old default, is moved to 16 once; any other saved value is kept. Settings
  files now carry a version marker for this. Old CHECK_RATE values are ignored.
- The dispatcher no longer leaks a slot if starting a check fails.

## 1.7.3

- The UPSTREAM panel showed Refused's own version. It now shows Remux's
  version (read from its public info endpoint) as REMUX_VERSION, and the row is
  left out if Remux doesn't report one. Refused's version stays in the header.

## 1.7.2

- Every module now has a first-run switch in `.env`: SEE_ALL_DEFAULT,
  SEARCH_CLEAN_DEFAULT and DIAGNOSTICS_DEFAULT join STREAM_STRIP_DEFAULT and
  POSTER_REFRESH_DEFAULT. They only set the starting state on the very first
  run; after that the panel's switches win.
- The panel's status note said "both modules off"; it now says "every module off".

## 1.7.0

- New SEARCH_CLEAN module (off by default). Infuse searches with two requests,
  one for titles (`/Items?searchTerm=`) and one for people (`/Persons?searchTerm=`).
  While enabled, the people request is answered with an empty list, so search
  shows titles only. Requests without a search term (browsing, a show's cast,
  tapping an actor) are not touched.

## 1.6.0

- SEE_ALL now delivers the list the way Infuse reads it. Infuse takes the
  length of the first response as the list's total, then asks for the
  remaining pages as you scroll and stacks every page after the first. Sending
  the whole list first made those pages repeat items, and answering them with
  empty pages made Infuse show "nothing to see here". Now the first response
  carries about half the list and the follow-up pages carry the rest, in
  order: nothing repeats and no page is empty. The split is computed per
  catalog and kept until the next "see all" for that catalog (last 20 kept).
- Removed the 30-minute window and the SAME_LIST_PAGES / HIDE_EXTRA_PAGES
  options; this is the only behaviour. A catalog of exactly 51 items has no
  clean split and is sent whole.

## 1.5.5

- SEE_ALL log now describes the delivered list by title, not just by Id: how
  many different titles it holds, which titles repeat (the same show or movie
  under a different Id) and where, plus the first 8 and last 12 entries. Use
  it to tell whether repeats come from Remux's list or from Infuse.

## 1.5.4

- SEE_ALL: removed the claim that Remux stops at about 500 items. How many
  come back is up to Remux; the LOG tab shows the count for each "see all".

## 1.5.3

- SEE_ALL: the empty pages from 1.5.2 made Infuse show "nothing to see here" at
  the end of a list. Follow-up page requests (for 30 minutes after "see all")
  are now cut from the exact list Infuse was just given, so they line up with
  it and are never empty. HIDE_EXTRA_PAGES is replaced by SAME_LIST_PAGES.

## 1.5.2

- SEE_ALL: new HIDE_EXTRA_PAGES option (on by default). After an expanded
  "see all" has delivered a whole catalog, Infuse still requests further pages
  while you scroll and appends them to the end, so items from the middle
  repeated. For 30 minutes those follow-up page requests for the same catalog
  now get an empty answer. Turn the option off to restore 1.5.1 behaviour.

## 1.5.1

- SEE_ALL: only the first page (`startIndex` 0 or absent) is expanded. Infuse
  keeps requesting further pages as you scroll; expanding those too sent it
  the same items again, so the end of the list repeated. The log line now
  also shows how many returned items are unique.

## 1.5.0

- New SEE_ALL module (off by default). Infuse asks Remux for 50 items when you
  tap "see all" on a home row, and the "latest items" lists have no total, so
  it stops there. SEE_ALL rewrites that one request (`/Items/Latest` with
  `limit=50`) to ask for everything (10,000), or for MAX_ITEMS if you set a
  cap. Home rows (20), favorites, search and all other requests are untouched.
  Each expansion is logged with how many items Remux returned.

## 1.4.0

- New DIAGNOSTICS toggle: a read-only request logger. While on, list-style
  JSON responses are written to the LOG tab with the Limit and StartIndex
  Infuse asked for and how many items Remux returned out of how many exist
  (to find out what Infuse sends when you tap "see all"). Bodies are not
  stored and tokens in URLs are masked. Off by default; JSON responses are
  buffered while it is on. Responses are never modified by it.

## 1.3.0

- Removed the CACHE_HEADERS option. Image requests are now always streamed
  straight through with Remux's own headers; Refused never buffers, hashes
  or alters an image on its way to Infuse. (Background poster checks still
  download images to fingerprint them.)

## 1.2.0

- Removed image resizing (SHRINK_ARTWORK and its three size/quality settings)
  and the Pillow dependency. Refused never modifies image bytes now; images
  stream straight through unless the optional cache headers are switched on.
- Settings files from 1.1.0 still load; the removed keys are dropped on the
  next save.

## 1.1.0 (audit)

Fixes
- Remux now advertises Refused's address, not its own: the client Host is sent
  as X-Forwarded-Host. Before, Jellyfin clients could save Remux's address
  and bypass the proxy.
- Parent artwork tags repeated on children (SeriesPrimaryImageTag,
  ParentBackdropImageTags, ParentThumbImageTag, ParentPrimaryImageTag,
  ParentLogoImageTag) now carry the same emitted tag as the parent, so one
  picture is never cached under two tags.
- The background checker survives unexpected errors instead of stopping
  silently and for good.
- Upstream dropping mid-body ends the response cleanly instead of raising
  through the ASGI server.
- Duplicate Date/Server headers removed.
- Expect header no longer forwarded.
- X-Forwarded-For appended, not replaced.
- With modules off, the client's Accept-Encoding is forwarded as-is, and no
  encoding is ever invented for a client that asked for none.
- Redirect rewriting respects the host:port boundary.
- Unknown /refused/... paths answer 404 instead of being sent to Remux.
- If-None-Match: * honoured.
- Login lockout sends Retry-After.
- Admin request bodies are capped while reading.
- httpx no longer logs one INFO line per proxied request.

Robustness
- JSON bodies over 256 KB are parsed and serialised in a worker thread.

Refactor
- SQLite persistence moved to poster_store.py.
- Ctx declares auth explicitly.
