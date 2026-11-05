// The Surveillance podcast (cloudflare SV1): its feed row comes from migration 0004, its episodes from the API
// (tg-tools' scheduled rss-audio jobs), and it is served only under its own keyed root.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import worker from '../src/index.js';
import { BASE, FEED_KEYS, KEY, SURVEILLANCE } from './keys.mjs';

const read = (name) => readFile(new URL(`../migrations/${name}`, import.meta.url), 'utf8');
const [feedGz, surveillance, feedItems] = await Promise.all([read('0002-feed-gz.sql'), read('0004-surveillance-feed.sql'),
  read('0005-feed-items.sql')]);

// The production schema of feeds/episodes predates the migrations folder; same tables as feed-store.test.mjs.
function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE feeds (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, author TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT 'my@email.com', language TEXT NOT NULL DEFAULT 'en-us', copyright TEXT NOT NULL DEFAULT '',
    logo_path TEXT NOT NULL, explicit TEXT NOT NULL DEFAULT 'no', site_url TEXT NOT NULL, feed_url TEXT NOT NULL,
    categories TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL DEFAULT '2026-10-01 00:00:00');
  CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, feed TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL,
    pub_date TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '', mp3_url TEXT NOT NULL,
    mp3_size INTEGER NOT NULL DEFAULT 0, duration TEXT, explicit TEXT NOT NULL DEFAULT 'no', keywords TEXT NOT NULL DEFAULT '',
    block TEXT NOT NULL DEFAULT 'no', season TEXT NOT NULL DEFAULT '', episode_num TEXT NOT NULL DEFAULT '',
    episode_type TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '2026-10-01 00:00:00',
    updated_at TEXT NOT NULL DEFAULT '2026-10-01 00:00:00', route TEXT, episode_kind TEXT, md_url TEXT, speech_text_url TEXT,
    manifest_url TEXT, source_md_url TEXT, UNIQUE(feed, slug));`);
  db.exec(feedGz);
  db.exec(surveillance);
  db.exec(feedItems);
  const statement = (sql, values) => ({
    async first() { return db.prepare(sql).get(...values) ?? null; },
    async all() { return { results: db.prepare(sql).all(...values) }; },
    async run() { return db.prepare(sql).run(...values); },
  });
  const env = { API_TOKEN: 'test', FEED_KEYS, DB: { prepare(sql) { return { bind: (...values) => statement(sql, values), ...statement(sql, []) }; } } };
  return { db, env };
}
const api = (env, method, path, body) => worker.fetch(new Request(`${BASE}${path}`, {
  method, headers: { authorization: 'Bearer test', 'content-type': 'application/json' }, body: body && JSON.stringify(body) }), env);

test('migration 0004 adds the Surveillance feed row once', () => {
  const { db } = fixture();
  db.exec(surveillance);
  const rows = db.prepare("SELECT id, title, logo_path, site_url FROM feeds WHERE id = 'surveillance'").all();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].title, 'Surveillance');
  db.close();
});

test('the API accepts surveillance episodes and the keyed feed publishes them with keyed links only', async () => {
  const { db, env } = fixture();
  const created = await api(env, 'POST', '/api/episodes', {
    feed: 'surveillance', slug: '2026-11-03-bloomberg-surveillance-radio', title: 'Bloomberg Surveillance Radio 11/3/2026',
    pub_date: '2026-11-03T12:00:00Z', mp3_url: `${BASE}/surveillance/episodes/2026-11-03-radio.mp3`, mp3_size: 10 });
  assert.equal(created.status, 201, await created.clone().text());
  const listed = await (await api(env, 'GET', '/api/feeds')).json();
  assert.ok(JSON.stringify(listed).includes('"surveillance"'));
  const response = await worker.fetch(new Request(`${SURVEILLANCE}/podcast.xml`), env);
  assert.equal(response.status, 200);
  const xml = await response.text();
  assert.match(xml, /<title>Surveillance<\/title>/);
  assert.match(xml, /<title>Bloomberg Surveillance Radio 11\/3\/2026<\/title>/);
  assert.ok(xml.includes(`<atom:link href="${SURVEILLANCE}/podcast.xml"`));
  assert.ok(xml.includes(`url="${SURVEILLANCE}/episodes/2026-11-03-radio.mp3"`));
  assert.ok(xml.includes(`${SURVEILLANCE}/assets/img/logo.png`));
  assert.match(xml, /<itunes:block>yes<\/itunes:block>/);
  assert.doesNotMatch(xml, /stations\.work\/surveillance\//, 'no storage-form URL leaks');
  db.close();
});

test('an unknown feed is still refused, and another podcast key does not open Surveillance', async () => {
  const { db, env } = fixture();
  const refused = await api(env, 'POST', '/api/episodes', { feed: 'nope', slug: 'x', title: 'x', pub_date: '2026-11-03', mp3_url: `${BASE}/x.mp3` });
  assert.equal(refused.status, 400);
  assert.match(await refused.text(), /feed must be one of audio, tts, surveillance/);
  assert.equal((await worker.fetch(new Request(`${BASE}/surveillance-${KEY.episodes}/podcast.xml`), env)).status, 404);
  db.close();
});

test('a generated feed lists every episode, newest first; blocked ones are left out (X-71 amendment: no cap)', async () => {
  const { db, env } = fixture();
  const insert = db.prepare("INSERT INTO episodes (feed, slug, title, pub_date, mp3_url, mp3_size, block) VALUES ('surveillance', ?, ?, ?, ?, 10, ?)");
  for (let day = 1; day <= 205; day++) {
    const date = new Date(Date.UTC(2026, 3, 1) + ((day * 37) % 205) * 86400e3).toISOString();
    insert.run(`show-${day}`, `Show ${day}`, date, `${BASE}/surveillance/episodes/show-${day}.mp3`, day % 41 === 0 ? 'yes' : 'no');
  }
  const xml = await (await worker.fetch(new Request(`${SURVEILLANCE}/podcast.xml`), env)).text();
  const titles = [...xml.matchAll(/<item>\s*<title>([^<]+)<\/title>/g)].map((m) => m[1]);
  const listed = db.prepare("SELECT title FROM episodes WHERE feed = 'surveillance' AND block != 'yes' ORDER BY pub_date DESC").all().map((r) => r.title);
  assert.equal(titles.length, 200, '205 episodes, 5 blocked');
  assert.deepEqual(titles, listed, 'every unblocked episode, newest first');
  assert.ok(!titles.includes('Show 41'), 'blocked: not listed');
  db.close();
});

test('an item links to its page_url, else its md_url, else its mp3 (X-76), keyed like every other URL', async () => {
  const { db, env } = fixture();
  const created = await api(env, 'POST', '/api/episodes', {
    feed: 'surveillance', slug: 'tv-11-04', title: 'TV 11/4', pub_date: '2026-11-04T12:00:00Z',
    mp3_url: `${BASE}/surveillance/episodes/tv-11-04.mp3`, md_url: `${BASE}/surveillance/episodes/tv-11-04.md`,
    page_url: `${BASE}/surveillance/episodes/tv-11-04/page.html` });
  assert.equal(created.status, 201, await created.clone().text());
  const { id, page_url } = await created.json();
  assert.equal(page_url, `${BASE}/surveillance/episodes/tv-11-04/page.html`);
  const link = async () => (await (await worker.fetch(new Request(`${SURVEILLANCE}/podcast.xml`), env)).text()).match(/<link>([^<]+)<\/link>\s*<pubDate>/)[1];
  assert.equal(await link(), `${SURVEILLANCE}/episodes/tv-11-04/page.html`);
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { page_url: null })).status, 200);
  assert.equal(await link(), `${SURVEILLANCE}/episodes/tv-11-04.md`);
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { md_url: null })).status, 200);
  assert.equal(await link(), `${SURVEILLANCE}/episodes/tv-11-04.mp3`);
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { page_url: 'page.html' })).status, 400, 'page_url must be absolute');
  db.close();
});
