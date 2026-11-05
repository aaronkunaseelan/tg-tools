// Test-only podcast keys (FEED_KEYS secret format) and the keyed public roots they give.
export const KEY = { episodes: 'E'.repeat(22), speech: 'S'.repeat(22), 'skeptic-tank': 'K'.repeat(22), surveillance: 'V'.repeat(22) };
export const FEED_KEYS = Object.entries(KEY).map(([name, key]) => `${name}=${key}`).join(',');
export const BASE = 'https://rss-feeds.stations.work';
export const EPISODES = `${BASE}/episodes-${KEY.episodes}`;
export const SPEECH = `${BASE}/speech-${KEY.speech}`;
export const SKEPTIC = `${BASE}/skeptic-tank-${KEY['skeptic-tank']}`;
export const SURVEILLANCE = `${BASE}/surveillance-${KEY.surveillance}`;
