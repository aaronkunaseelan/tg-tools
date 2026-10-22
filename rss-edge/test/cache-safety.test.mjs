import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';

const base = 'https://rss-edge.05802.workers.dev';
const rssEnv = { B2_RSS_BUCKET: 'rss-endpoints-private-05802', B2_RSS_KEY_ID: 'id', B2_RSS_KEY: 'key' };
const originalFetch = globalThis.fetch;

const authResponse = (token) => Response.json({
  authorizationToken: token,
  apiInfo: { storageApi: { downloadUrl: 'https://download.example' } }
});

test('an expired B2 token is replaced and the object request is replayed once', async () => {
  const calls = [];
  let issued = 0;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, token: init.headers?.Authorization });
    if (url.includes('b2_authorize_account')) return authResponse(`token-${++issued}`);
    if (init.headers.Authorization === 'token-1') {
      return new Response('{"code":"expired_auth_token"}', { status: 401 });
    }
    return new Response('audio', { status: 200, headers: { 'content-type': 'audio/mpeg' } });
  };
  try {
    const response = await worker.fetch(new Request(`${base}/audio/example.mp3`), rssEnv);
    assert.equal(response.status, 200);
    assert.equal(issued, 2, 'reauthorized instead of returning the origin 401');
    const objectCalls = calls.filter((call) => !call.url.includes('b2_authorize_account'));
    assert.deepEqual(objectCalls.map((call) => call.token), ['token-1', 'token-2']);
  } finally { globalThis.fetch = originalFetch; }
});

test('a persistent origin 401 is surfaced without a third attempt', async () => {
  let objectAttempts = 0;
  globalThis.fetch = async (url) => {
    if (url.includes('b2_authorize_account')) return authResponse('token');
    objectAttempts += 1;
    return new Response(null, { status: 401 });
  };
  try {
    const response = await worker.fetch(new Request(`${base}/audio/example.mp3`), rssEnv);
    assert.equal(response.status, 401);
    assert.equal(objectAttempts, 2);
    assert.equal(response.headers.get('cache-control'), 'no-store');
  } finally { globalThis.fetch = originalFetch; }
});

test('token-gated and operational paths are never storable in a shared cache', async () => {
  const env = { ...rssEnv, API_TOKEN: 'secret', DB: { prepare: () => ({ all: async () => ({ results: [{ id: 'audio' }] }) }) } };
  const cases = [
    new Request(`${base}/api/feeds`),
    new Request(`${base}/api/feeds`, { headers: { authorization: 'Bearer secret' } }),
    new Request(`${base}/admin`),
    new Request(`${base}/newsl/feed.xml`),
    new Request(`${base}/healthz`)
  ];
  for (const request of cases) {
    const response = await worker.fetch(request, env);
    assert.equal(response.headers.get('cache-control'), 'private, no-store', request.url + ' ' + response.status);
  }
});

test('generated feeds stay publicly cacheable', async () => {
  const feed = { id: 'audio', title: 't', description: 'd', author: 'a', email: 'e@x', language: 'en-us',
    copyright: '', logo_path: 'assets/img/logo.png', explicit: 'no', site_url: `${base}/audio`,
    feed_url: `${base}/audio/podcast.xml`, categories: '[]', updated_at: '2026-09-26 00:00:00' };
  const env = { DB: { prepare: () => ({ bind: () => ({
    first: async () => feed, all: async () => ({ results: [] }), run: async () => ({})
  }) }) } };
  const response = await worker.fetch(new Request(`${base}/audio/podcast.xml`), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'public, s-maxage=300, max-age=60');
});
