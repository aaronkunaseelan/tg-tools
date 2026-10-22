import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import worker from '../src/index.js';

const migration = await readFile(new URL('../migrations/0002-feed-gz.sql', import.meta.url), 'utf8');

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
  db.exec(migration);
  db.prepare("INSERT INTO feeds (id, title, description, author, logo_path, site_url, feed_url) VALUES ('audio', 'Audio', 'Talks', 'A', 'audio/assets/img/logo.png', 'https://rss-edge.05802.workers.dev/audio', 'https://rss-edge.05802.workers.dev/audio/podcast.xml')").run();
  db.prepare("INSERT INTO episodes (feed, slug, title, pub_date, mp3_url, mp3_size) VALUES ('audio', 'one', 'First talk', '2026-10-02', 'https://rss-edge.05802.workers.dev/audio/episodes/one.mp3', 10)").run();
  const queries = [];
  const statement = (sql, values) => ({
    async first() { return db.prepare(sql).get(...values) ?? null; },
    async all() { return { results: db.prepare(sql).all(...values) }; },
    async run() { return db.prepare(sql).run(...values); },
  });
  const env = { API_TOKEN: 'test', DB: { prepare(sql) { queries.push(sql); return { bind: (...values) => statement(sql, values), ...statement(sql, []) }; } } };
  return { db, env, queries };
}
const get = (env, headers = {}) => worker.fetch(new Request('https://rss-edge.05802.workers.dev/audio/podcast.xml', { headers }), env);
const api = (env, method, path, body) => worker.fetch(new Request(`https://rss-edge.05802.workers.dev${path}`, {
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
  assert.ok(!queries.some(q => q.startsWith('SELECT * FROM episodes')), 'unchanged source: no episode load');
  db.prepare("UPDATE episodes SET title = 'Edited in D1', updated_at = '2026-10-03 00:00:00'").run(); // outside the API
  db.prepare('UPDATE feed_gz SET built_at = built_at - 86400').run();
  assert.match(await (await get(env)).text(), /Edited in D1/);
  db.prepare("UPDATE feed_gz SET version = 'old'").run();
  queries.length = 0;
  await (await get(env)).text();
  assert.ok(queries.some(q => q.startsWith('SELECT * FROM episodes')), 'new presentation: rendered again');
  db.close();
});

test('unknown feeds stay 404 and store nothing', async () => {
  const { db, env } = fixture();
  db.prepare('DELETE FROM feeds').run();
  assert.equal((await get(env)).status, 404);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM feed_gz').get().n, 0);
  db.close();
});
