// The storage -> keyed-URL mapping (P5b; X-68). tg-tools has its half in
// shared/tg_tools_common/podcast_urls.py (STORAGE_HOSTS, PODCASTS); each repo's suite checks the other's
// (test/podcast-map.test.mjs here, shared/tests/test_podcast_urls.py there), so a host or podcast changed on
// one side only fails a test instead of giving Telegram links that 404.
//
// Every host this Worker has served from: stored URLs on any of them are rewritten.
export const STORED_HOSTS = [
  "rss-feeds.stations.work", "rss.stations.work", "rss-b2-proxy.aaronkuna.workers.dev",
  "rss-b2-proxy.05802.workers.dev", "rss-edge.05802.workers.dev"
];
// Podcasts are served only under /<name>-<key>/: nothing in them is reachable without the key.
// `prefix` is where its objects sit in B2; `feed` is its D1 feed id (none: a static podcast.xml in B2).
export const PODCASTS = [
  { name: "episodes", feed: "audio", prefix: "audio" },
  { name: "speech", feed: "tts", prefix: "tts" },
  { name: "skeptic-tank", feed: null, prefix: "skeptic-tank" },
  // Bloomberg Surveillance, TV and radio full shows, published by tg-tools' scheduled rss-audio jobs (cloudflare SV1).
  { name: "surveillance", feed: "surveillance", prefix: "surveillance" }
];
