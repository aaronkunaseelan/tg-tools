import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';

const base = 'https://rss-edge.05802.workers.dev';
const env = { B2_RSS_BUCKET: 'rss-endpoints-private-05802', B2_RSS_KEY_ID: 'id', B2_RSS_KEY: 'key' };
const originalFetch = globalThis.fetch;

test('private origin forwards GET/HEAD/Range but not credentials or B2 headers', async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (url.includes('b2_authorize_account')) return Response.json({ authorizationToken: 'private-token', apiInfo: { storageApi: { downloadUrl: 'https://download.example' } } });
    return new Response(init.method === 'HEAD' ? null : 'audio', { status: init.headers.range ? 206 : 200,
      headers: { 'content-range': 'bytes 0-4/100', 'accept-ranges': 'bytes', 'content-type': 'audio/mpeg', 'x-bz-file-id': 'secret-id' } });
  };
  try {
    const response = await worker.fetch(new Request(`${base}/audio/example.mp3`, { headers: { Range: 'bytes=0-4' } }), env);
    assert.equal(response.status, 206);
    assert.equal(response.headers.get('content-range'), 'bytes 0-4/100');
    assert.equal(response.headers.get('x-bz-file-id'), null);
    assert.equal(calls[1].init.headers.Authorization, 'private-token');
    assert.equal(calls[1].init.headers.range, 'bytes=0-4');
    assert.match(calls[1].url, /\/file\/rss-endpoints-private-05802\/audio\/example.mp3$/);
    const head = await worker.fetch(new Request(`${base}/audio/example.mp3`, { method: 'HEAD' }), env);
    assert.equal(head.status, 200);
    assert.equal(calls.at(-1).init.method, 'HEAD');
  } finally { globalThis.fetch = originalFetch; }
});

test('origin rate limits are not marked cacheable', async () => {
  globalThis.fetch = async () => new Response('{"code":"too_many_requests"}', { status: 429, headers: { 'content-type': 'application/json' } });
  try {
    const res = await worker.fetch(new Request(`${base}/audio/example.mp3`), env);
    assert.equal(res.status, 429);
    assert.equal(res.headers.get('cache-control'), 'no-store');
  } finally { globalThis.fetch = originalFetch; }
});

test('missing RSS credentials fail closed without anonymous origin request', async () => {
  let called = false;
  globalThis.fetch = () => { called = true; throw new Error('unexpected fetch'); };
  try {
    await assert.rejects(worker.fetch(new Request(`${base}/audio/example.mp3`), { B2_RSS_BUCKET: env.B2_RSS_BUCKET }), /not configured/);
    assert.equal(called, false);
  } finally { globalThis.fetch = originalFetch; }
});

test('generated feed uses the sole hostname for its feed and old-host enclosures', async () => {
  const feed = { id: 'audio', site_url: 'https://rss.stations.work', feed_url: 'https://rss.stations.work/audio/podcast.xml', logo_path: 'logo.png', categories: '[]', author: 'Author', email: 'a@example.com', title: 'Title', description: 'Description', language: 'en', copyright: 'Copyright', explicit: 'no', updated_at: '1' };
  const episode = { feed: 'audio', slug: 'one', title: 'One', pub_date: '2026-01-01', description: 'Description', excerpt: 'Excerpt', mp3_url: 'https://rss-b2-proxy.aaronkuna.workers.dev/audio/one.mp3', mp3_size: 5, explicit: 'no', block: 'no', keywords: '', updated_at: '1' };
  const db = { prepare: (sql) => ({ bind: () => ({ first: async () => sql.includes('MAX(') ? { count: 1, latest_updated: '1' } : feed, all: async () => ({ results: [episode] }), run: async () => ({}) }) }) };
  const res = await worker.fetch(new Request(`${base}/audio/podcast.xml`), { DB: db });
  const xml = await res.text();
  assert.match(xml, /rss-edge\.05802\.workers\.dev\/audio\/one\.mp3/);
  assert.match(xml, /rss-edge\.05802\.workers\.dev\/audio\/podcast\.xml/);
  assert.doesNotMatch(xml, /rss\.stations\.work|rss-b2-proxy/);
});
