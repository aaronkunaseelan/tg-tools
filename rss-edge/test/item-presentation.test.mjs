// SV-condensed build 4: the description text once (content:encoded), a one-sentence description/itunes:summary,
// and <podcast:chapters> on the condensed edition's items.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import worker from '../src/index.js';
import { BASE, FEED_KEYS, SURVEILLANCE } from './keys.mjs';

const read = (name) => readFile(new URL(`../migrations/${name}`, import.meta.url), 'utf8');
const [feedGz, surveillance, feedItems] = await Promise.all([read('0002-feed-gz.sql'), read('0004-surveillance-feed.sql'),
  read('0005-feed-items.sql')]);

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
  for (const migration of [feedGz, surveillance, feedItems]) db.exec(migration);
  const statement = (sql, values) => ({
    async first() { return db.prepare(sql).get(...values) ?? null; },
    async all() { return { results: db.prepare(sql).all(...values) }; },
    async run() { return db.prepare(sql).run(...values); },
  });
  const env = { API_TOKEN: 'test', FEED_KEYS, DB: { prepare(sql) { return { bind: (...values) => statement(sql, values), ...statement(sql, []) }; } } };
  return { db, env };
}

const add = (db, row) => {
  const values = { feed: 'surveillance', mp3_size: 10, block: 'no', explicit: 'no', keywords: '', ...row };
  const keys = Object.keys(values);
  db.prepare(`INSERT INTO episodes (${keys.join(', ')}) VALUES (${keys.map(() => '?').join(', ')})`).run(...keys.map((key) => values[key]));
};
const feed = async (env) => (await worker.fetch(new Request(`${SURVEILLANCE}/podcast.xml`), env)).text();
const cdataOf = (xml, tag) => {
  const value = xml.match(new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`))?.[1];
  return value?.replace(/^<!\[CDATA\[([\s\S]*?)\]\]>$/, '$1');
};

const notes = `<h3>1:02 · Manish Kabra of Société Générale</h3>
<p>First sentence of the notes, which is the excerpt.</p>
<p>A second paragraph that must never be repeated in the description.</p>`;

test('the full text is emitted once, in content:encoded; description and itunes:summary are one sentence', async () => {
  const { db, env } = fixture();
  add(db, { slug: 'notes-item', title: 'Notes item', pub_date: '2026-11-05T12:00:00Z',
    mp3_url: `${BASE}/surveillance/episodes/notes-item.mp3`, description: notes });
  const xml = await feed(env);
  const item = xml.match(/<item>[\s\S]*?<\/item>/)[0];
  assert.equal((item.match(/A second paragraph that must never be repeated in the description\./g) || []).length, 1);
  assert.equal((item.match(/<h3>1:02 · Manish Kabra of Société Générale<\/h3>/g) || []).length, 1);
  assert.equal(cdataOf(item, 'content:encoded'), notes, 'the notes, HTML and all, in the field apps render');
  assert.equal(cdataOf(item, 'description'), 'First sentence of the notes, which is the excerpt.');
  assert.equal(cdataOf(item, 'itunes:summary'), cdataOf(item, 'description'), 'the legacy field gets the same sentence');
  assert.doesNotMatch(cdataOf(item, 'description'), /<|second paragraph/);
  assert.equal(cdataOf(item, 'itunes:subtitle'), '', 'the stored excerpt field is untouched');
  db.close();
});

test("the publisher's excerpt wins; without one, the first sentence of the text, capped at 300 characters", async () => {
  const { db, env } = fixture();
  add(db, { slug: 'set', title: 'Set excerpt', pub_date: '2026-11-05T12:00:00Z',
    mp3_url: `${BASE}/surveillance/episodes/set.mp3`, description: notes, excerpt: 'Set by the publisher.' });
  const long = `${'A clause with extra words and detail, '.repeat(20)}and then a period.`;
  add(db, { slug: 'long', title: 'Long text', pub_date: '2026-11-05T11:00:00Z',
    mp3_url: `${BASE}/surveillance/episodes/long.mp3`, description: `<p>${long}</p><p>Not this one.</p>` });
  const xml = await feed(env);
  const itemOf = (title) => xml.match(new RegExp(`<item>\\s*<title>${title}</title>[\\s\\S]*?</item>`))[0];
  assert.equal(cdataOf(itemOf('Set excerpt'), 'description'), 'Set by the publisher.');
  const capped = cdataOf(itemOf('Long text'), 'description');
  assert.ok(capped.length <= 300, `capped at 300, got ${capped.length}`);
  assert.ok(capped.startsWith('A clause with extra words'), capped);
  assert.doesNotMatch(capped, /Not this one/);
  assert.doesNotMatch(capped, /<p>/);
  db.close();
});

test('a condensed item gets <podcast:chapters>; a full show page and a page-less item do not', async () => {
  const { db, env } = fixture();
  add(db, { slug: 'tv-condensed', title: 'TV — interviews', pub_date: '2026-11-05T12:01:00Z',
    mp3_url: `${BASE}/surveillance/episodes/tv-condensed.mp3`, page_url: `${SURVEILLANCE}/episodes/tv-condensed.page.html` });
  add(db, { slug: 'tv', title: 'TV', pub_date: '2026-11-05T12:00:00Z',
    mp3_url: `${BASE}/surveillance/episodes/tv.mp3`, page_url: `${SURVEILLANCE}/episodes/tv.page.html` });
  add(db, { slug: 'plain', title: 'No page', pub_date: '2026-11-05T11:00:00Z',
    mp3_url: `${BASE}/surveillance/episodes/plain.mp3`, md_url: `${BASE}/surveillance/episodes/plain.md` });
  const xml = await feed(env);
  const chapters = [...xml.matchAll(/<podcast:chapters url="([^"]+)" type="application\/json\+chapters"\/>/g)];
  assert.equal(chapters.length, 1, 'one condensed item, one element');
  assert.equal(chapters[0][1], `${SURVEILLANCE}/episodes/tv-condensed.chapters.json`);
  assert.match(xml, /<rss[^>]*xmlns:podcast="https:\/\/podcastindex\.org\/namespace\/1\.0"/);
  assert.doesNotMatch(xml, /<podcast:(?!chapters)/, 'no other podcast namespace tags');
  db.close();
});

test('a storage-form page URL becomes the keyed chapters URL too, and a versioned name keeps its stem', async () => {
  const { db, env } = fixture();
  add(db, { slug: 'storage', title: 'Storage page', pub_date: '2026-11-05T12:02:00Z',
    mp3_url: `${BASE}/surveillance/episodes/storage.mp3`, page_url: `${BASE}/surveillance/episodes/storage-condensed.page.html` });
  add(db, { slug: 'versioned', title: 'Versioned page', pub_date: '2026-11-05T12:01:00Z',
    mp3_url: `${BASE}/surveillance/episodes/versioned.mp3`, page_url: `${SURVEILLANCE}/episodes/versioned-condensed.v2.page.html` });
  const xml = await feed(env);
  const urls = [...xml.matchAll(/<podcast:chapters url="([^"]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(urls, [`${SURVEILLANCE}/episodes/storage-condensed.chapters.json`,
    `${SURVEILLANCE}/episodes/versioned-condensed.v2.chapters.json`]);
  assert.doesNotMatch(xml, /stations\.work\/surveillance\//, 'no storage-form URL leaks');
  db.close();
});

test('the stored item fragment is the same presentation, chapters included', async () => {
  const { db, env } = fixture();
  const created = await worker.fetch(new Request(`${BASE}/api/episodes`, { method: 'POST',
    headers: { authorization: 'Bearer test', 'content-type': 'application/json' },
    body: JSON.stringify({ feed: 'surveillance', slug: 'stored', title: 'Stored', pub_date: '2026-11-05T12:00:00Z',
      mp3_url: `${BASE}/surveillance/episodes/stored.mp3`, description: notes,
      page_url: `${BASE}/surveillance/episodes/stored-condensed.page.html` }) }), env);
  assert.equal(created.status, 201, await created.clone().text());
  const fragment = db.prepare('SELECT xml FROM feed_items').get().xml;
  const description = cdataOf(fragment, 'description');
  assert.doesNotMatch(description, /second paragraph|<h3>/);
  assert.ok(description.length <= 300, description);
  assert.match(fragment, /<content:encoded><!\[CDATA\[<h3>/);
  assert.match(fragment, /<podcast:chapters url="[^"]+\/stored-condensed\.chapters\.json"/);
  const xml = await feed(env);
  assert.ok(xml.includes(fragment.trim()), 'a rebuild joins from the fragment');
  db.close();
});

test('the validator\'s vocabulary rules: explicit true|false, no empty season/episode/episodeType, subtitle <= 255', async () => {
  const { db, env } = fixture();
  add(db, { slug: 'vocab', title: 'Vocab', pub_date: '2026-11-05T12:00:00Z', mp3_url: `${BASE}/surveillance/episodes/vocab.mp3`,
    explicit: 'no', excerpt: 'A subtitle sentence that goes on and on. '.repeat(10) });
  add(db, { slug: 'numbered', title: 'Numbered', pub_date: '2026-11-05T11:00:00Z', mp3_url: `${BASE}/surveillance/episodes/n.mp3`,
    explicit: 'yes', season: '2', episode_num: '7', episode_type: 'bonus' });
  const xml = await feed(env);
  assert.equal((xml.match(/<itunes:explicit>false<\/itunes:explicit>/g) || []).length, 2, 'channel and the clean item');
  const numbered = xml.match(/<item>\s*<title>Numbered<\/title>[\s\S]*?<\/item>/)[0];
  assert.match(numbered, /<itunes:explicit>true<\/itunes:explicit>/);
  assert.match(numbered, /<itunes:season>2<\/itunes:season>/);
  assert.match(numbered, /<itunes:episode>7<\/itunes:episode>/);
  assert.match(numbered, /<itunes:episodeType>bonus<\/itunes:episodeType>/);
  const vague = xml.match(/<item>\s*<title>Vocab<\/title>[\s\S]*?<\/item>/)[0];
  assert.doesNotMatch(vague, /<itunes:season|<itunes:episode>|<itunes:episodeType>/);
  const subtitle = cdataOf(vague, 'itunes:subtitle');
  assert.ok(subtitle.length <= 255, String(subtitle.length));
  assert.doesNotMatch(subtitle, / $/);
  db.close();
});
