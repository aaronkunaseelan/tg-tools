-- One rendered <item> per episode (X-71 amendment, 2026-11-04): rendered when the episode is written through the API,
-- joined under a fresh channel header when a feed is rebuilt. `version` is the presentation (FEED_PRESENTATION),
-- `context` a fingerprint of the keyed root, logo and author it was rendered with, `source_updated_at` the episode's
-- updated_at at render. A fragment that differs on any of them is rendered again at the next rebuild. The table
-- can be dropped and refilled at any time.
CREATE TABLE IF NOT EXISTS feed_items (
  episode_id INTEGER PRIMARY KEY,
  version TEXT NOT NULL,
  context TEXT NOT NULL,
  source_updated_at TEXT,
  xml TEXT NOT NULL
);
-- An episode's reader page (X-76 amendment: Surveillance notes + script), written by tg-tools and set through the API.
-- When present it is the item's <link>. Not idempotent: wrangler applies each migration once.
ALTER TABLE episodes ADD COLUMN page_url TEXT;
