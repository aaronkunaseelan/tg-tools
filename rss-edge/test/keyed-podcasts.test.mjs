import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { BASE, FEED_KEYS, KEY, EPISODES, SPEECH, SKEPTIC, SURVEILLANCE } from './keys.mjs';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';

const env = { B2_RSS_BUCKET: 'bucket', B2_RSS_KEY_ID: 'id', B2_RSS_KEY: 'key', FEED_KEYS };
const originalFetch = globalThis.fetch;
function origin(body = 'object') {
  const objects = [];
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('b2_authorize_account')) return Response.json({
      authorizationToken: 't', apiInfo: { storageApi: { downloadUrl: 'https://download.example' } }
    });
    objects.push(String(url).replace('https://download.example/file/bucket/', ''));
    return new Response(init.method === 'HEAD' ? null : body, { headers: { 'content-type': 'audio/mpeg' } });
  };
  return objects;
}

test('a keyed path serves only its own podcast storage', async () => {
  const objects = origin();
  try {
    for (const [url, key] of [[`${EPISODES}/episodes/a.mp3`, 'audio/episodes/a.mp3'], [`${SPEECH}/episodes/x/a.mp3`, 'tts/episodes/x/a.mp3'],
      [`${SKEPTIC}/mp3/a.mp3`, 'skeptic-tank/mp3/a.mp3'], [`${EPISODES}/assets/img/logo.png`, 'audio/assets/img/logo.png'],
      [`${SURVEILLANCE}/episodes/a.mp3`, 'surveillance/episodes/a.mp3']]) {
      const response = await worker.fetch(new Request(url), env);
      assert.equal(response.status, 200, url);
      assert.equal(objects.at(-1), key);
    }
  } finally { globalThis.fetch = originalFetch; }
});

test('an episode page (…page.html) is passed through from B2 as sandboxed HTML; other objects keep their type', async () => {
  const page = '<!doctype html><p>Notes: https://rss.stations.work/surveillance/episodes/a.mp3</p>';
  const objects = origin(page);
  try {
    const response = await worker.fetch(new Request(`${SURVEILLANCE}/episodes/2026-11-04-tv/page.html`), env);
    assert.equal(response.status, 200);
    assert.equal(objects.at(-1), 'surveillance/episodes/2026-11-04-tv/page.html');
    assert.equal(response.headers.get('content-type'), 'text/html; charset=utf-8');
    assert.match(response.headers.get('content-security-policy'), /^sandbox\b/);
    assert.doesNotMatch(response.headers.get('content-security-policy'), /allow-scripts|allow-same-origin/);
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    assert.equal(await response.text(), page, 'no rewrite, no render');
    const head = await worker.fetch(new Request(`${SURVEILLANCE}/episodes/2026-11-04-tv/page.html`, { method: 'HEAD' }), env);
    assert.equal(head.headers.get('content-type'), 'text/html; charset=utf-8');
    const other = await worker.fetch(new Request(`${SURVEILLANCE}/episodes/a.mp3`), env);
    assert.equal(other.headers.get('content-type'), 'audio/mpeg');
    assert.equal(other.headers.get('content-security-policy'), 'sandbox allow-popups allow-popups-to-escape-sandbox');
    assert.equal(other.headers.get('x-content-type-options'), 'nosniff');
  } finally { globalThis.fetch = originalFetch; }
});

test('the sandbox is decided on the decoded name B2 is asked for, and every keyed passthrough carries it', async () => {
  const objects = origin('<p>x</p>');
  try {
    for (const name of ['x.page%2Ehtml', 'x.page.htm%6C', 'x.page.html']) {
      const response = await worker.fetch(new Request(`${SURVEILLANCE}/episodes/${name}`), env);
      assert.equal(objects.at(-1), 'surveillance/episodes/x.page.html', name);
      assert.equal(response.headers.get('content-type'), 'text/html; charset=utf-8', name);
      assert.equal(response.headers.get('content-security-policy'), 'sandbox allow-popups allow-popups-to-escape-sandbox', name);
      assert.equal(response.headers.get('x-content-type-options'), 'nosniff', name);
    }
    for (const [name, type] of [['a.mp3', 'audio/mpeg'], ['a.xml', 'audio/mpeg'], ['a.txt', 'audio/mpeg']]) {
      const response = await worker.fetch(new Request(`${EPISODES}/episodes/${name}`), env);
      assert.equal(response.headers.get('content-type'), type, 'its own content type');
      assert.equal(response.headers.get('content-security-policy'), 'sandbox allow-popups allow-popups-to-escape-sandbox', name);
      assert.equal(response.headers.get('x-content-type-options'), 'nosniff', name);
    }
  } finally { globalThis.fetch = originalFetch; }
});

test('wrong, swapped, short or missing keys, traversal, and bare storage paths are 404 before the origin', async () => {
  const objects = origin();
  try {
    const paths = [
      `/episodes-${'X'.repeat(22)}/podcast.xml`, `/episodes-${KEY.speech}/podcast.xml`, `/speech-${KEY.episodes}/episodes/a.mp3`,
      `/episodes-${KEY.episodes.slice(1)}/podcast.xml`, `/skeptic-${KEY['skeptic-tank']}/podcast.xml`, `/episodes-${'X'.repeat(22)}`,
      `/episodes-${KEY.episodes}/../tts/x.mp3`, `/episodes-${KEY.episodes}/%2e%2e/tts/x.mp3`, `/episodes-${KEY.episodes}/a/%E0%A4%A`,
      '/audio/podcast.xml', '/tts/podcast.xml', '/skeptic-tank/podcast.xml', '/audio/episodes/a.mp3', '/tts/episodes/x/a.mp3',
      '/skeptic-tank/mp3/a.mp3', '/surveillance/podcast.xml', '/surveillance/episodes/a.mp3', `/surveillance-${KEY.episodes}/podcast.xml`, '/archive/tg-tools/x', '/_thrptest/x', '/', '/index.html'
    ];
    for (const path of paths) assert.equal((await worker.fetch(new Request(BASE + path), env)).status, 404, path);
    for (const missing of [{ ...env, FEED_KEYS: undefined }, { ...env, FEED_KEYS: 'episodes=short' }, { ...env, FEED_KEYS: `speech=${KEY.speech}` }]) {
      assert.equal((await worker.fetch(new Request(`${EPISODES}/episodes/a.mp3`), missing)).status, 404);
    }
    assert.deepEqual(objects, []);
  } finally { globalThis.fetch = originalFetch; }
});

test('webfeeds and reader objects still come straight from B2', async () => {
  const objects = origin();
  try {
    for (const path of ['/webfeeds/imf-notes.xml', '/reader/abc']) {
      assert.equal((await worker.fetch(new Request(BASE + path), env)).status, 200, path);
    }
    assert.deepEqual(objects, ['webfeeds/imf-notes.xml', 'reader/abc']);
  } finally { globalThis.fetch = originalFetch; }
});

test('a keyed root sends you to its feed; a wrong key still 404s', async () => {
  for (const root of [EPISODES, `${EPISODES}/`, SKEPTIC]) {
    const response = await worker.fetch(new Request(root), env);
    assert.equal(response.status, 302, root);
    assert.equal(response.headers.get('location'), `${root.replace(/\/$/, '')}/podcast.xml`);
  }
  assert.equal((await worker.fetch(new Request(`${BASE}/speech-${KEY.episodes}`), env)).status, 404);
});

const migration = await readFile(new URL('../migrations/0002-feed-gz.sql', import.meta.url), 'utf8');
function staticFixture(keys = FEED_KEYS) {
  const db = new DatabaseSync(':memory:');
  db.exec(migration);
  const statement = (sql, values) => ({
    async first() { return db.prepare(sql).get(...values) ?? null; },
    async run() { return db.prepare(sql).run(...values); },
  });
  return { db, env: { ...env, FEED_KEYS: keys, DB: { prepare: (sql) => ({ bind: (...values) => statement(sql, values) }) } } };
}
function staticOrigin(state) {
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('b2_authorize_account')) return Response.json({
      authorizationToken: 't', apiInfo: { storageApi: { downloadUrl: 'https://download.example' } }
    });
    state.calls.push({ url: String(url).replace('https://download.example/file/bucket/', ''), inm: init.headers['if-none-match'] });
    if (state.fail) return new Response('down', { status: 503 });
    if (init.headers['if-none-match'] === state.etag) return new Response(null, { status: 304 });
    return new Response(state.body, { headers: { etag: state.etag, 'content-type': 'application/rss+xml' } });
  };
}
const STATIC = '<rss><channel><itunes:image href="https://rss.stations.work/skeptic-tank/assets/logo.png"/>' +
  '<item><enclosure url="https://rss-edge.05802.workers.dev/skeptic-tank/mp3/a.mp3"/></item></channel></rss>';

test('the static skeptic-tank feed is rewritten once, stored, and published with its keyed URLs', async () => {
  const { db, env: e } = staticFixture();
  const state = { calls: [], etag: '"v1"', body: STATIC };
  staticOrigin(state);
  try {
    const first = await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`), e);
    const xml = await first.text();
    assert.deepEqual(state.calls.map((c) => c.url), ['skeptic-tank/podcast.xml']);
    assert.ok(xml.includes(`${SKEPTIC}/assets/logo.png`) && xml.includes(`${SKEPTIC}/mp3/a.mp3`));
    assert.doesNotMatch(xml, /workers\.dev|rss\.stations\.work|stations\.work\/skeptic-tank\//);
    assert.match(xml.split('<item>')[0], /<itunes:block>yes<\/itunes:block>/);
    assert.equal(first.headers.get('content-type'), 'application/rss+xml; charset=utf-8');
    assert.equal(first.headers.get('cache-control'), 'public, s-maxage=300, max-age=60');
    assert.doesNotMatch(first.headers.get('etag'), new RegExp(KEY['skeptic-tank']), 'the ETag carries a fingerprint, not the key');
    // Within the check interval: served from the stored copy, no origin request; a matching ETag is a 304.
    const again = await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`, { headers: { 'accept-encoding': 'gzip' } }), e);
    assert.equal(again.headers.get('content-encoding'), 'gzip');
    assert.equal((await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`, { headers: { 'if-none-match': first.headers.get('etag') } }), e)).status, 304);
    assert.equal(state.calls.length, 1);
    // After the interval: a conditional request; unchanged (304) keeps the copy, changed (new ETag) rebuilds it.
    db.prepare("UPDATE feed_gz SET built_at = built_at - 301").run();
    assert.equal(await (await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`), e)).text(), xml);
    assert.equal(state.calls.at(-1).inm, '"v1"');
    db.prepare("UPDATE feed_gz SET built_at = built_at - 301").run();
    Object.assign(state, { etag: '"v2"', body: STATIC.replace('a.mp3', 'b.mp3') });
    assert.ok((await (await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`), e)).text()).includes(`${SKEPTIC}/mp3/b.mp3`));
    // Origin down with a stored copy: the copy is served.
    db.prepare("UPDATE feed_gz SET built_at = built_at - 301").run();
    state.fail = true;
    const down = await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`), e);
    assert.equal(down.status, 200);
    assert.ok((await down.text()).includes('b.mp3'));
  } finally { globalThis.fetch = originalFetch; }
});

test('a new skeptic-tank key rebuilds the stored copy; origin down with no copy is not cached', async () => {
  const { db, env: e } = staticFixture();
  const state = { calls: [], etag: '"v1"', body: STATIC };
  staticOrigin(state);
  try {
    await (await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`), e)).text();
    const other = 'Z'.repeat(22);
    const rotated = { ...e, FEED_KEYS: FEED_KEYS.replace(KEY['skeptic-tank'], other) };
    const xml = await (await worker.fetch(new Request(`${BASE}/skeptic-tank-${other}/podcast.xml`), rotated)).text();
    assert.equal(state.calls.length, 2, 'rebuilt for the new key');
    assert.ok(xml.includes(`${BASE}/skeptic-tank-${other}/mp3/a.mp3`) && !xml.includes(KEY['skeptic-tank']));
    db.exec('DELETE FROM feed_gz');
    state.fail = true;
    const down = await worker.fetch(new Request(`${SKEPTIC}/podcast.xml`), e);
    assert.equal(down.status, 503);
    assert.equal(down.headers.get('cache-control'), 'no-store');
  } finally { globalThis.fetch = originalFetch; }
});
