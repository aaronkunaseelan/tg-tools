# rss-edge recovered Worker

Cloudflare service `rss-b2-proxy` was renamed in place to `rss-edge` on 2026-08-29. The sole RSS delivery hostname is `https://rss-edge.05802.workers.dev`. The `rss.stations.work/*` Worker route was removed on 2026-09-26; existing subscriptions to that hostname now fail rather than redirect.

The source was originally recovered from deployed Cloudflare Worker version `152dfe04-d088-4580-997c-48e6d9f6b263` on 2026-07-20 because the original source repository was unavailable.

The Worker requires HTTP Basic authentication for the entire `/newsl` namespace before any route lookup or B2 request. Credentials are Cloudflare encrypted secrets named `NEWSL_RSS_USERNAME` and `NEWSL_RSS_PASSWORD`; values must never be added here.

Newsl is deprecated and deferred. Its authenticated `/newsl` behavior remains unchanged; the separate `newsl-private-05802` bucket uses `B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY` secrets.

Public RSS paths are delivered by the Worker from **private** bucket `rss-endpoints-private-05802` using bucket-scoped read-only secrets `B2_RSS_KEY_ID` and `B2_RSS_KEY`. Never set these in `wrangler.toml` or commit them. The original `rss-endpoints` bucket was also made private and retained as a rollback copy; neither bucket allows anonymous B2 downloads. No anonymous-origin fallback exists. Audio and TTS cover art comes from `project-txt/icon-draft-dark.png` and `project-txt/icon-draft-dark1.png` respectively, center-cropped and resized to 1600×1600 PNG, stored at `audio/assets/img/logo.png` and `tts/assets/img/logo.png` in the private bucket. The asset paths are not stripped by the Worker. Skeptic Tank art remains unchanged. The Worker forwards GET/HEAD and conditional/ranged requests and returns only selected public headers. Audio/TTS XML generated from D1 and the static skeptic-tank XML rewrite legacy delivery hosts in responses; D1 rows and the stored static XML were **not** modified. One audio episode whose enclosure points at third-party `pscrb.fm` is intentionally omitted from the published feed (158 D1 items, 157 published), pending a rights-approved import of that media. Its row is retained in D1.

`src/index.js` is the recovered Wrangler bundle, not the original TypeScript source tree. `node --test test/*.test.mjs` exercises the private-origin and feed rules.

## Pinned dependencies and commands

Node 24.20.0 and npm 12.0.2 are required and checked against `.node-version`,
exact engines and `packageManager` metadata; Wrangler is locked exactly to
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
every episode row. Any successful API write (`POST`/`PUT`/`DELETE` under `/api/`) drops the stored
copies and the next GET rebuilds. A copy older than a day, or from another presentation
version (`FEED_PRESENTATION`, the ETag's presentation key), is checked again: unchanged
sources keep their bytes, so D1 edits made outside the API appear within a day.
`<pubDate>`/`<lastBuildDate>` on the channel are the build time, not the request time.
Publishers and the episode API are unchanged; `rss-episodes` stays the episode store.

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
`description` and `content:encoded` carry simple HTML; `itunes:summary` has a
separately rendered plain-text fallback with labelled URLs, not stripped link labels.
URLs deduplicate after legacy delivery-host canonicalization, preserving meaningful
queries/fragments. The feed ETag includes `listener-description-v2`; increment this
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
