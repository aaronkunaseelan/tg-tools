import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import worker from '../src/index.js';
import { FEED_KEYS, EPISODES, SPEECH, KEY } from './keys.mjs';

const read = (name) => readFile(new URL(`../migrations/${name}`, import.meta.url), 'utf8');
const migrations = await Promise.all([read('0002-feed-gz.sql'), read('0005-feed-items.sql')]);

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
  for (const migration of migrations) db.exec(migration);
  db.prepare("INSERT INTO feeds (id, title, description, author, logo_path, site_url, feed_url) VALUES ('audio', 'Audio', 'Talks', 'A', 'audio/assets/img/logo.png', 'https://rss-edge.05802.workers.dev/audio', 'https://rss-edge.05802.workers.dev/audio/podcast.xml')").run();
  db.prepare("INSERT INTO episodes (feed, slug, title, pub_date, mp3_url, mp3_size) VALUES ('audio', 'one', 'First talk', '2026-10-02', 'https://rss-edge.05802.workers.dev/audio/episodes/one.mp3', 10)").run();
  const queries = [];
  // Item renders, counted where they are stored: each fragment upsert binds a JSON list of [id, updated_at, xml].
  const renders = [];
  const statement = (sql, values) => ({
    async first() { return db.prepare(sql).get(...values) ?? null; },
    async all() { return { results: db.prepare(sql).all(...values) }; },
    async run() {
      if (sql.includes('INSERT OR REPLACE INTO feed_items')) renders.push(...JSON.parse(values[2]).map(([id]) => id));
      return db.prepare(sql).run(...values);
    },
  });
  const env = { API_TOKEN: 'test', FEED_KEYS, DB: { prepare(sql) { queries.push(sql); return { bind: (...values) => statement(sql, values), ...statement(sql, []) }; } } };
  return { db, env, queries, renders };
}
const get = (env, headers = {}) => worker.fetch(new Request(`${EPISODES}/podcast.xml`, { headers }), env);
const api = (env, method, path, body) => worker.fetch(new Request(`https://rss-feeds.stations.work${path}`, {
  method, headers: { authorization: 'Bearer test', 'content-type': 'application/json' }, body: body && JSON.stringify(body) }), env);

test('the first GET renders and stores; later GETs read one row and return the same bytes and ETag', async () => {
  const { db, env, queries } = fixture();
  const first = await get(env);
  assert.equal(first.status, 200);
  const body = await first.text();
  assert.match(body, /<title>First talk<\/title>/);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 1);
  queries.length = 0;
  const second = await get(env);
  assert.equal(await second.text(), body);
  assert.equal(second.headers.get('etag'), first.headers.get('etag'));
  assert.equal(second.headers.get('content-type'), 'application/rss+xml; charset=utf-8');
  assert.deepEqual(queries, ['SELECT version, etag, gz, built_at FROM feed_gz WHERE feed = ?1']);
  const notModified = await get(env, { 'if-none-match': first.headers.get('etag') });
  assert.equal(notModified.status, 304);
  assert.equal(notModified.headers.get('etag'), first.headers.get('etag'));
  db.close();
});

test('clients that accept gzip get the stored gzip bytes as-is; the rest get plain XML', async () => {
  const { db, env } = fixture();
  const plain = await (await get(env)).text();
  const zipped = await get(env, { 'accept-encoding': 'br, gzip, deflate' });
  assert.equal(zipped.headers.get('content-encoding'), 'gzip');
  assert.equal(zipped.headers.get('vary'), 'Accept-Encoding');
  const bytes = new Uint8Array(await zipped.arrayBuffer());
  assert.deepEqual([...bytes.subarray(0, 2)], [0x1f, 0x8b]);
  assert.equal(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text(), plain);
  db.close();
});

test('an API write drops the stored copies and the next GET shows the change', async () => {
  const { db, env } = fixture();
  const before = await get(env);
  await before.text();
  const id = db.prepare("SELECT id FROM episodes WHERE slug = 'one'").get().id;
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { title: 'Renamed talk' })).status, 200);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 0);
  const after = await get(env);
  assert.match(await after.text(), /<title>Renamed talk<\/title>/);
  assert.notEqual(after.headers.get('etag'), before.headers.get('etag'));
  // Reads and rejected writes keep the stored copy.
  await api(env, 'GET', '/api/feeds');
  await api(env, 'PUT', '/api/episodes/999999', { title: 'x' });
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 1);
  db.close();
});

test('a day-old or other-version copy is checked again; an unchanged source keeps its bytes', async () => {
  const { db, env, queries } = fixture();
  const body = await (await get(env)).text();
  db.prepare('UPDATE feed_gz SET built_at = built_at - 86400').run();
  queries.length = 0;
  assert.equal(await (await get(env)).text(), body);
  assert.ok(!queries.some(q => q.startsWith('SELECT * FROM episodes') || q.includes('LEFT JOIN feed_items')), 'unchanged source: no episode load');
  db.prepare("UPDATE episodes SET title = 'Edited in D1', updated_at = '2026-10-03 00:00:00'").run(); // outside the API
  db.prepare('UPDATE feed_gz SET built_at = built_at - 86400').run();
  assert.match(await (await get(env)).text(), /Edited in D1/);
  db.prepare("UPDATE feed_gz SET version = 'old'").run();
  queries.length = 0;
  await (await get(env)).text();
  assert.ok(queries.some(q => q.includes('LEFT JOIN feed_items')), 'new presentation: built again (from stored items)');
  db.close();
});

test('unknown feeds stay 404 and store nothing', async () => {
  const { db, env } = fixture();
  db.prepare('DELETE FROM feeds').run();
  assert.equal((await get(env)).status, 404);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 0);
  db.close();
});

// Each item is rendered once, when it is written, and stored in feed_items; a rebuild joins the stored items.
const addEpisodes = (db, n) => {
  const insert = db.prepare("INSERT INTO episodes (feed, slug, title, pub_date, mp3_url, mp3_size, description) VALUES ('audio', ?, ?, ?, ?, 10, ?)");
  for (let i = 1; i <= n; i++) {
    insert.run(`ep-${i}`, `Episode ${i}`, `2026-10-${String(3 + (i % 25)).padStart(2, '0')}T${String(i % 24).padStart(2, '0')}:00:00Z`,
      `https://rss.stations.work/audio/episodes/ep-${i}.mp3`, `Talk ${i}. See https://rss-edge.05802.workers.dev/audio/notes/ep-${i}.md for notes.`);
  }
};
// The channel's own <pubDate>/<lastBuildDate> are the build time; everything else must match byte for byte.
const sameBuild = (xml) => xml.replace(/(<(pubDate|lastBuildDate)>)[^<]*(<\/\2>)(?=[\s\S]*<image>)/g, '$1BUILD$3');
async function fullRender(db, env) {
  db.exec('DELETE FROM feed_items; DELETE FROM feed_gz');
  return (await get(env)).text();
}

test('an API write renders that one item; the rebuild after it renders nothing and equals a full render', async () => {
  const { db, env, renders } = fixture();
  addEpisodes(db, 12);
  await (await get(env)).text();
  assert.equal(renders.length, 13, 'first build: every item rendered once');
  renders.length = 0;
  const id = db.prepare("SELECT id FROM episodes WHERE slug = 'ep-5'").get().id;
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { title: 'Renamed five' })).status, 200);
  assert.deepEqual(renders, [id], 'the write renders exactly its own item');
  const created = await (await api(env, 'POST', '/api/episodes', { feed: 'audio', slug: 'new', title: 'Brand new',
    pub_date: '2026-11-01T00:00:00Z', mp3_url: 'https://rss-feeds.stations.work/audio/episodes/new.mp3' })).json();
  assert.deepEqual(renders, [id, created.id]);
  renders.length = 0;
  const rebuilt = await (await get(env)).text();
  assert.deepEqual(renders, [], 'the rebuild renders nothing');
  assert.match(rebuilt, /<title>Renamed five<\/title>/);
  assert.match(rebuilt.split('<item>')[1], /<title>Brand new<\/title>/, 'newest first');
  const full = await fullRender(db, env);
  assert.equal(renders.length, 14);
  assert.equal(sameBuild(rebuilt), sameBuild(full));
  db.close();
});

test('a key rotation or a presentation change re-renders every item once', async () => {
  const { db, env, renders } = fixture();
  addEpisodes(db, 5);
  await (await get(env)).text();
  renders.length = 0;
  const other = 'R'.repeat(22);
  const rotated = { ...env, FEED_KEYS: env.FEED_KEYS.replace(/episodes=[^,]+/, `episodes=${other}`) };
  const url = `https://rss-feeds.stations.work/episodes-${other}/podcast.xml`;
  const xml = await (await worker.fetch(new Request(url), rotated)).text();
  assert.equal(renders.length, 6, 'new key: every item once');
  assert.ok(xml.includes(`/episodes-${other}/episodes/ep-1.mp3`) && !xml.includes(EPISODES));
  db.exec('DELETE FROM feed_gz');
  renders.length = 0;
  await (await worker.fetch(new Request(url), rotated)).text();
  assert.equal(renders.length, 0, 'and not again');
  db.exec("UPDATE feed_items SET version = 'listener-description-v5-newest-40'; DELETE FROM feed_gz");
  await (await worker.fetch(new Request(url), rotated)).text();
  assert.equal(renders.length, 6, 'old presentation: every item once');
  db.exec('DELETE FROM feed_gz');
  renders.length = 0;
  await (await worker.fetch(new Request(url), rotated)).text();
  assert.equal(renders.length, 0);
  db.close();
});

test('an episode edited outside the API is re-rendered at the next rebuild; a deleted one leaves no fragment', async () => {
  const { db, env, renders } = fixture();
  addEpisodes(db, 4);
  await (await get(env)).text();
  renders.length = 0;
  db.prepare("UPDATE episodes SET title = 'Fixed in D1', updated_at = '2026-11-04 12:00:00' WHERE slug = 'ep-2'").run();
  db.prepare('UPDATE feed_gz SET built_at = built_at - 86400').run();
  const xml = await (await get(env)).text();
  const id = db.prepare("SELECT id FROM episodes WHERE slug = 'ep-2'").get().id;
  assert.deepEqual(renders, [id]);
  assert.match(xml, /<title>Fixed in D1<\/title>/);
  assert.equal(sameBuild(xml), sameBuild(await fullRender(db, env)));
  assert.equal((await api(env, 'DELETE', `/api/episodes/${id}`)).status, 200);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_items WHERE episode_id = ?').get(id).n, 0);
  assert.doesNotMatch(await (await get(env)).text(), /Fixed in D1/);
  db.close();
});

test('an unpublishable enclosure is stored as an empty fragment and never rendered again', async () => {
  const { db, env, renders } = fixture();
  db.prepare("INSERT INTO episodes (feed, slug, title, pub_date, mp3_url) VALUES ('audio', 'ext', 'External', '2026-10-05', 'https://pscrb.fm/x.mp3')").run();
  const xml = await (await get(env)).text();
  assert.doesNotMatch(xml, /External/);
  assert.equal(db.prepare("SELECT xml FROM feed_items i JOIN episodes e ON e.id = i.episode_id WHERE e.slug = 'ext'").get().xml, '');
  db.exec('DELETE FROM feed_gz');
  renders.length = 0;
  assert.equal(sameBuild(await (await get(env)).text()), sameBuild(xml), 'same feed');
  assert.equal(renders.length, 0);
  db.close();
});

test('a feed edit that items show (author) re-renders them once; a title edit renders none', async () => {
  const { db, env, renders } = fixture();
  addEpisodes(db, 3);
  await (await get(env)).text();
  renders.length = 0;
  assert.equal((await api(env, 'PUT', '/api/feeds/audio', { title: 'Audio, renamed' })).status, 200);
  assert.match(await (await get(env)).text(), /<title>Audio, renamed<\/title>/);
  assert.equal(renders.length, 0, 'the channel header is built fresh every time');
  assert.equal((await api(env, 'PUT', '/api/feeds/audio', { author: 'Someone Else' })).status, 200);
  const xml = await (await get(env)).text();
  assert.equal(renders.length, 4);
  assert.equal(xml.split('<item>').slice(1).filter((item) => item.includes('<itunes:author>Someone Else</itunes:author>')).length, 4);
  db.close();
});

// A fixture with a second feed, so a write's reach can be told from every feed's.
function twoFeeds() {
  const f = fixture();
  f.db.prepare("INSERT INTO feeds (id, title, description, author, logo_path, site_url, feed_url) VALUES ('tts', 'Speech', 'Spoken', 'A', 'tts/assets/img/logo.png', 'https://rss-edge.05802.workers.dev/tts', 'https://rss-edge.05802.workers.dev/tts/podcast.xml')").run();
  f.db.prepare("INSERT INTO episodes (feed, slug, title, pub_date, mp3_url, mp3_size) VALUES ('tts', 'spoken', 'A spoken one', '2026-10-02', 'https://rss-edge.05802.workers.dev/tts/episodes/spoken.mp3', 10)").run();
  f.getTts = () => worker.fetch(new Request(`${SPEECH}/podcast.xml`), f.env);
  f.stored = () => f.db.prepare('SELECT feed FROM feed_gz ORDER BY feed').all().map((row) => row.feed);
  return f;
}

test('an API write drops only the stored copy of its own feed (both, for a move); a feed edit drops that feed', async () => {
  const { db, env, getTts, stored } = twoFeeds();
  await (await get(env)).text();
  await (await getTts()).text();
  assert.deepEqual(stored(), ['audio', 'tts']);
  const id = db.prepare("SELECT id FROM episodes WHERE slug = 'one'").get().id;
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { title: 'Renamed talk' })).status, 200);
  assert.deepEqual(stored(), ['tts'], 'the Speech copy stays');
  assert.match(await (await get(env)).text(), /<title>Renamed talk<\/title>/);
  assert.equal((await api(env, 'POST', '/api/episodes', { feed: 'audio', slug: 'two', title: 'Second',
    pub_date: '2026-10-03T00:00:00Z', mp3_url: 'https://rss-feeds.stations.work/audio/episodes/two.mp3' })).status, 201);
  assert.deepEqual(stored(), ['tts']);
  await (await get(env)).text();
  assert.equal((await api(env, 'PUT', `/api/episodes/${id}`, { feed: 'tts' })).status, 200);
  assert.deepEqual(stored(), [], 'a move drops both feeds');
  assert.match(await (await getTts()).text(), /<title>Renamed talk<\/title>/);
  await (await get(env)).text();
  assert.equal((await api(env, 'DELETE', `/api/episodes/${id}`)).status, 200);
  assert.deepEqual(stored(), ['audio']);
  assert.doesNotMatch(await (await getTts()).text(), /Renamed talk/);
  await (await get(env)).text();
  assert.equal((await api(env, 'PUT', '/api/feeds/tts', { title: 'Speech, renamed' })).status, 200);
  assert.deepEqual(stored(), ['audio']);
  db.close();
});

// Fail every statement that touches feed_items (a D1 error, or migration 0005 missing).
// Returns what to call to give the table back.
const failFeedItems = (env, when = () => true) => {
  const prepare = env.DB.prepare;
  env.DB.prepare = function (sql) {
    if (sql.includes('feed_items') && when(sql)) throw new Error('D1 feed_items failure');
    return prepare.call(this, sql);
  };
  return () => { env.DB.prepare = prepare; };
};
const failing = async (env, write) => { const restore = failFeedItems(env, (sql) => !sql.startsWith('SELECT')); try { return await write(); } finally { restore(); } };

test('the fragment step never fails a committed API write, and the stored copy is still dropped', async () => {
  const quiet = console.error;
  console.error = () => {};
  try {
    const { db, env } = fixture();
    await (await get(env)).text();
    const created = await failing(env, () => api(env, 'POST', '/api/episodes', { feed: 'audio', slug: 'new', title: 'Brand new',
      pub_date: '2026-11-01T00:00:00Z', mp3_url: 'https://rss-feeds.stations.work/audio/episodes/new.mp3' }));
    assert.equal(created.status, 201);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 0);
    const id = (await created.json()).id;
    assert.match(await (await get(env)).text(), /<title>Brand new<\/title>/);
    assert.equal((await failing(env, () => api(env, 'PUT', `/api/episodes/${id}`, { title: 'Edited new' }))).status, 200);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 0);
    assert.match(await (await get(env)).text(), /<title>Edited new<\/title>/);
    assert.equal((await failing(env, () => api(env, 'DELETE', `/api/episodes/${id}`))).status, 200);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 0);
    assert.doesNotMatch(await (await get(env)).text(), /Edited new/);
    db.close();
  } finally { console.error = quiet; }
});

test('rotating another podcast\'s key re-renders an item that links its storage URL', async () => {
  const { db, env, renders } = fixture();
  db.prepare("UPDATE episodes SET description = 'See https://rss-edge.05802.workers.dev/tts/episodes/x/a.md' WHERE slug = 'one'").run();
  const before = await (await get(env)).text();
  assert.ok(before.includes(`/speech-${KEY.speech}/episodes/x/a.md`));
  renders.length = 0;
  const other = 'Q'.repeat(22);
  const rotated = { ...env, FEED_KEYS: env.FEED_KEYS.replace(/speech=[^,]+/, `speech=${other}`) };
  db.exec('DELETE FROM feed_gz');
  const after = await (await worker.fetch(new Request(`${EPISODES}/podcast.xml`), rotated)).text();
  assert.equal(renders.length, 1, 'the Episodes item is rendered again');
  assert.ok(after.includes(`/speech-${other}/episodes/x/a.md`) && !after.includes(KEY.speech));
  db.close();
});

test('fragments are stored in chunks of about 200 KB, so a killed first render keeps its progress', async () => {
  const { db, env, renders, queries } = fixture();
  addEpisodes(db, 199);
  db.prepare("UPDATE episodes SET description = ?").run('é'.repeat(900)); // multi-byte: bytes, not characters, count
  const upserts = () => queries.filter((q) => q.includes('INSERT OR REPLACE INTO feed_items')).length;
  await (await get(env)).text();
  assert.equal(renders.length, 200);
  assert.ok(upserts() >= 3 && upserts() < 50, `${upserts()} upserts`);
  for (const row of db.prepare('SELECT length(CAST(xml AS BLOB)) AS n FROM feed_items').all()) assert.ok(row.n < 20000);
  // Kill the first render after its second upsert: what was stored stays, and the next render does only the rest.
  db.exec('DELETE FROM feed_items; DELETE FROM feed_gz');
  renders.length = 0;
  const run = env.DB.prepare.bind(env.DB);
  let seen = 0;
  env.DB.prepare = (sql) => {
    if (sql.includes('INSERT OR REPLACE INTO feed_items') && ++seen === 3) throw new Error('killed');
    return run(sql);
  };
  await assert.rejects(get(env), /killed/);
  const kept = db.prepare('SELECT COUNT(*) AS n FROM feed_items').get().n;
  assert.ok(kept > 0 && kept < 200, `${kept} kept`);
  env.DB.prepare = run;
  renders.length = 0;
  const xml = await (await get(env)).text();
  assert.equal(renders.length, 200 - kept, 'only the rest');
  db.exec('DELETE FROM feed_gz');
  assert.equal(xml.split('<item>').length - 1, 200);
  db.close();
});
