# rss-feeds Worker (recovered rss-edge bundle)

Cloudflare service `rss-b2-proxy` was renamed in place to `rss-edge` on 2026-08-29, and to `rss-feeds` on 2026-11-03 (cloudflare P5b). Its one hostname is `https://rss-feeds.stations.work` (custom domain in `wrangler.toml`); `workers_dev` and preview URLs are off.

**Podcasts are served only under a keyed path**, `/<name>-<key>/`: `/<name>-<key>/podcast.xml` is the feed and `/<name>-<key>/<path>` the B2 object `<prefix>/<path>`. An object whose path ends in `page.html` (an episode's reader page, X-76; its URL is the episode's `page_url`, which when set is the item's `<link>`, before `md_url` and the MP3) is passed through unchanged as `text/html; charset=utf-8` with `Content-Security-Policy: sandbox` (no scripts, opaque origin: `/admin` keeps its API token in this origin's `localStorage`). The table is `PODCASTS` in `src/index.js`: `episodes` (D1 feed `audio`, B2 `audio/`), `speech` (feed `tts`, B2 `tts/`) and `skeptic-tank` (a static `podcast.xml` in B2 `skeptic-tank/`). Keys live only in the Worker secret `FEED_KEYS` (`name=key` pairs, comma-separated, 22–64 URL-safe characters) and the owner's 0600 configs; a podcast without a valid key is not served. Bare storage paths (`/audio/…`, `/tts/…`, `/skeptic-tank/…`), `archive/`, `_thrptest/` and every other path are 404, with no redirect. Stored URLs on any of this Worker's past or present hosts, in fields and inside description text, are rewritten to the keyed path when a feed is rendered, so D1 doesn't change. Feeds carry `<itunes:block>yes</itunes:block>` so podcast directories don't list them. To rotate a key, change `FEED_KEYS` and resubscribe; the stored feed copies rebuild because their ETag includes a fingerprint of the key.

On `rss-feeds.stations.work`, a single-segment path matching `[a-z0-9-]{8,80}` exactly (no `.xml` suffix) is a reader feed: the private bucket's `reader/<slug>` object, through the same authenticated origin proxy, with generated-feed caching (`public, s-maxage=300, max-age=60`). Besides the keyed podcasts, only `/webfeeds/…` and `/reader/…` come straight from B2; `/api/`, `/admin`, `/newsl` and `/healthz` are routed as below.

The source was originally recovered from deployed Cloudflare Worker version `152dfe04-d088-4580-997c-48e6d9f6b263` on 2026-07-20 because the original source repository was unavailable.

The Worker requires HTTP Basic authentication for the entire `/newsl` namespace before any route lookup or B2 request. Credentials are Cloudflare encrypted secrets named `NEWSL_RSS_USERNAME` and `NEWSL_RSS_PASSWORD`; values must never be added here.

Newsl is deprecated and deferred. Its authenticated `/newsl` behavior remains unchanged; the separate `newsl-private-05802` bucket uses `B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY` secrets.

Public RSS paths are delivered by the Worker from **private** bucket `rss-endpoints-private-05802` using bucket-scoped read-only secrets `B2_RSS_KEY_ID` and `B2_RSS_KEY`. Never set these in `wrangler.toml` or commit them. The original `rss-endpoints` bucket was also made private and retained as a rollback copy; neither bucket allows anonymous B2 downloads. No anonymous-origin fallback exists. Audio and TTS cover art comes from `project-txt/icon-draft-dark.png` and `project-txt/icon-draft-dark1.png` respectively, center-cropped and resized to 1600×1600 PNG, stored at `audio/assets/img/logo.png` and `tts/assets/img/logo.png` in the private bucket. The asset paths are not stripped by the Worker. Skeptic Tank art remains unchanged. The Worker forwards GET/HEAD and conditional/ranged requests and returns only selected public headers. Audio/TTS XML generated from D1 and the static skeptic-tank XML rewrite legacy delivery hosts in responses; D1 rows and the stored static XML were **not** modified. One audio episode whose enclosure points at third-party `pscrb.fm` is intentionally omitted from the published feed (158 D1 items, 157 published), pending a rights-approved import of that media. Its row is retained in D1.

`src/index.js` is the recovered Wrangler bundle, not the original TypeScript source tree. `node --test test/*.test.mjs` exercises the private-origin and feed rules.

## Pinned dependencies and commands

Node 26 and npm 12 (major versions, `engines` in `package.json`) are required; exact host
versions live in agent-tools-update's `runtimes.toml`. Wrangler is locked exactly to
4.147.0 in `package-lock.json`. Do not use global Wrangler or `npx`.

```bash
npm run deps:setup
npm run check                 # syntax and existing Node tests
npm test
npm run build                 # deploy --dry-run; bundles stay host-local
npm run deploy -- --strict    # only with the existing authenticated environment
```

External setup step: supply the host-owned `worker-deps.py` at `../../scripts/worker-deps.py` before using the npm scripts (it is not bundled here).

`../../scripts/worker-deps.py` uses a real cached npm 12.0.2 CLI (never a
Corepack download shim) and `npm ci --ignore-scripts`. Both locked platform
artifacts must exist, be executable and match exact versions before host-local
hardlinks materialize tool binaries. No upstream installers/download fallbacks
run; missing artifacts fail before READY. Tree/version gates then mark it ready. Dependencies are content/runtime-addressed under
`~/.local/lib/workbench-workers/`, never source `node_modules` or host-path symlinks.
Changed manifests, locks, Node/npm versions or platform require `deps:setup` again;
missing/inconsistent locks and failed installs fail closed. Treat ready environments
as immutable; do not edit them. Logs, cache and dry-run bundles are host-local.
Installs, native checks, tests and dry runs use a minimal credential-free
environment, isolated HOME and distinct empty npm user/global configs. Explicit
errors enforce safety even with Python `-O`. Only explicit deployment or other
remote `wrangler` commands inherit the existing authenticated environment.

For cwd-independent use (substitute the local workbench root):

```bash
python3 /path/to/scripts/worker-deps.py --project /path/to/rss-edge wrangler --version
```

The 4.147.0 release updates Miniflare and uses workerd 1.20261001.1 and esbuild
0.28.1. CLI fixes concern Containers and Workflow reads, not this Worker's
routing/authentication. Compatibility date and cache settings are unchanged.
[Official release notes](https://github.com/cloudflare/workers-sdk/releases/tag/wrangler%404.147.0).

Before future deployments, verify both RSS secrets exist, test full and ranged MP3 reads, HEAD, generated and stored XML, and that direct anonymous B2 downloads fail. The original bucket is a private rollback copy; don't delete it without a retention decision. Newsl delivery authority remains governed separately and is not part of this RSS cutover.

## Cache posture

`/api/*`, `/admin*`, `/newsl*` and `/healthz` answer `private, no-store`; generated feeds
answer `public, s-maxage=300, max-age=60`. Keep it that way. Cloudflare's cache key holds
neither `Authorization` nor `Cookie`, and Workers Caching gives a 200 with no
`Cache-Control` two hours of heuristic freshness — an unsealed token-gated response would
be servable to anonymous callers the moment `[cache] enabled` is set.

Workers Caching (`[cache] enabled = true`, Wrangler ≥ 4.69) is **not** enabled. It works on
workers.dev, unlike the legacy Cache API. Before enabling it, know that Cloudflare then
strips `Range` from the request, asks this Worker for the full object, and does the byte
slicing itself — a Worker-produced `206` is never stored, and the in-Worker range
forwarding becomes redundant. See
`project-txt/2026-09-26-rss-edge-hardening-and-workers-cache-research.md`.

B2 object reads replay once against a freshly authorized token when the origin answers
401, so a revoked or expired cached token does not 401 podcast clients for the life of the
isolate.

## Stored feeds (2026-10-18)

Audio and TTS feeds are rendered once per change and stored gzip-compressed (base64 text)
in D1 `feed_gz` (migrations `0002`; `0001`'s plain `feed_xml` was dropped by `0003`, since
decoding a 930 KB row cost 7–14 ms). A feed GET reads that one row and returns the stored
bytes with the same ETag as before: as-is with `Content-Encoding: gzip` when the client
accepts gzip, decompressed otherwise (`Vary: Accept-Encoding`). No per-request render over
every episode row. A successful API episode write drops the stored copy of that episode's feed
(both feeds, for an update that moves it) and a feed edit (`/api/feeds/<id>`) drops that feed's;
the other feeds' copies stay, and the next GET rebuilds. A copy older than a day, or from another presentation
version (`FEED_PRESENTATION`, the ETag's presentation key), is checked again: unchanged
sources keep their bytes, so D1 edits made outside the API appear within a day.
`<pubDate>`/`<lastBuildDate>` on the channel are the build time, not the request time.
Publishers and the episode API are unchanged; `rss-episodes` stays the episode store.

Generated feeds list every non-blocked episode, newest first (X-71 amendment, 2026-11-04: the
40-item cap is gone). Each episode's `<item>` is rendered once, when the API creates or updates
it, and stored in D1 `feed_items` (migration `0005`) with the presentation version, a fingerprint
of what the item takes from its feed (keyed root, logo, author) and the episode's `updated_at`. A
rebuild joins the stored items under a freshly built channel header; an item missing or differing
on any of those three is rendered then and stored, so a key rotation, presentation bump or feed
author/logo edit renders every item once, and an edit made in D1 outside the API is picked up as
long as it bumps `updated_at`. `generateXml` (every item rendered now) is the reference the stored
path must equal byte for byte. The API delete removes the episode's item; a row deleted outside the
API leaves an unread `feed_items` row. The stored copy of the full Episodes feed (177 episodes,
4 Nov) is 176 KB of base64 gzip, under a tenth of D1's 2 MB row limit, ~1 KB per item.

The fragment step can never fail a write: after the episode row is committed, a failed fragment
render or delete is logged and the fragment dropped (or left, where it no longer matches the row's
`updated_at`), and the stored feed copy is dropped regardless. Fragments are upserted in chunks of
about 200 KB (UTF-8 bytes of the JSON), so a first render that is killed keeps what it stored and the
next one renders only the rest. An item's context also fingerprints every podcast's key, so any key
rotation re-renders every fragment once.

**Known race (accepted):** `updated_at` has one-second resolution. Two API writes to the same
episode within the same second can leave the stored fragment holding the first write's render with
a `source_updated_at` equal to the row's, so the rebuild does not see it as stale and the feed shows
the first write's fields until the episode is next edited, the presentation version bumps or a key
rotates. The notes job makes one write per episode; two writers to one episode in one second is
the case. Closing it would take a monotonic `revision` column or a hash of the rendered fields.

## Deploy: migration 0005 first

Migration `0005-feed-items.sql` (the `feed_items` table and `episodes.page_url`) must be on the
database before the Worker that reads it is deployed: without it every generated feed fails to
rebuild (`no such table: feed_items`) and every API write hits the fragment step's error path.
Apply it with `wrangler d1 execute rss-episodes --remote --file migrations/0005-feed-items.sql`
(through `worker-deps.py wrangler`), not `migrations apply`: earlier migrations went in with
`execute`, so `d1_migrations` doesn't list them, and `migrations apply` would replay 0001–0004 and
asks for confirmation. Then verify both exist, and only then deploy:

    SELECT name FROM sqlite_master WHERE name = 'feed_items';
    SELECT page_url FROM episodes LIMIT 1;

The migration is not idempotent (`ALTER TABLE ... ADD COLUMN`): if the execute fails after the
`CREATE TABLE`, check whether `page_url` exists before running it again. Rolling back to the
previous Worker after 0005 is safe: the old code ignores `feed_items` and `page_url`.

## Listener description layout

Generated audio/TTS descriptions are presented without rewriting D1 rows or
artifacts. `src/episode-description.js` recognizes the existing plain-text
summary/Topics/artifact-footer contract and renders:

- deduplicated **Notes · Transcript · Original** links first (available roles only);
- a whole-sentence summary preview (up to two paragraphs, targeting 120 words /
  900 characters; a single longer first sentence is preserved rather than cut);
- up to six topic bullets, with the complete topic list and evidence in Notes;
- known original date/duration and a machine-generated summary label.

MP3 and Manifest links are omitted from the listener description. Full descriptions
remain stored; full script/notes/manifest objects, GUIDs and enclosures are unchanged.
Publisher HTML outside the recognized generated contract is not summarized.
The full text is emitted **once**, in `content:encoded` — the notes, or the publisher's HTML — because that
is the field podcast apps render in full (Apple Podcasts and Pocket Casts read it as HTML). `description`
and the legacy `itunes:summary` carry the **one-sentence excerpt** instead, as escaped text (not
CDATA): the publisher's `excerpt` when set, else the first sentence of the text, HTML stripped, at most
300 characters. `itunes:subtitle` keeps the stored excerpt, clipped to its 255-character limit. This is what keeps the feed small: the same text used to be emitted three times
(573,856 characters of description in a 627,385-byte Surveillance feed).

`<podcast:chapters>` (Podcast Namespace 1.0, `type="application/json+chapters"`) is emitted on an item
whose page is a condensed edition's — a `<stem>-condensed[.vN].page.html` name. Its URL is the sibling
`<stem>-condensed[.vN].chapters.json` that the condensed publisher uploads with its six artefacts (chapters
JSON version 1.2.0: `startTime`, `title`). A full show's page has no chapters file, so its item carries
no element. The rule is one naming contract: all six artefacts share the `<slug>-condensed` stem.

The item also follows the iTunes vocabulary the feed validator checks: `itunes:explicit` is `true`/`false`
(the stored field is `yes`/`no`), and `itunes:season`, `itunes:episode` and `itunes:episodeType` are emitted
only when they have a value.
URLs deduplicate after legacy delivery-host canonicalization, preserving meaningful
queries/fragments. The feed ETag includes the presentation key (`FEED_PRESENTATION` in `src/index.js`); increment this
presentation key whenever output changes so unchanged D1 metadata cannot cause a
stale 304. Pocket Casts' server-side/app cache may still delay existing episode updates.

## Episode lookup contract

Publication callers use `GET /api/episodes?feed=audio&slug=<identity>` (or
`feed=tts`) for bound exact equality. `search` remains fuzzy admin search;
its complete UTF-8 LIKE pattern, including two `%` characters, must fit
50 bytes. Oversized search and conflicting `slug`/`search` selectors return
400 before querying D1. Search length never silently selects exact lookup.

Deploy this API with the matching RSS and TTS callers as one coordinated
release: drain both publication workers before switching the API, then
switch their immutable releases and resume. Older callers still send full
slugs through `search` and are incompatible with the new search validation.
No D1 migration or new service is needed. Qualify the original 49-character
slug through a read-only production GET after promotion before any replay.
