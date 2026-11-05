-- The Surveillance podcast (cloudflare SV1, 2026-11-03): Bloomberg Surveillance TV and radio full shows, owner-only.
-- Served only under /surveillance-<key>/ (FEED_KEYS); objects sit under surveillance/ in the RSS bucket.
-- The API can update a feed row but not create one, so the row comes from here. Idempotent.
INSERT OR IGNORE INTO feeds (id, title, description, author, email, language, copyright, logo_path, explicit,
  site_url, feed_url, categories, updated_at)
VALUES ('surveillance', 'Surveillance', 'Bloomberg Surveillance, TV and radio: the full shows, a personal copy', '05802',
  'my@email.com', 'en-us', '', 'assets/img/logo.png', 'no', 'https://rss-feeds.stations.work/surveillance',
  'https://rss-feeds.stations.work/surveillance/podcast.xml', '["News > Business News"]', datetime('now'));
