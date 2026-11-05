import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';

const base = 'https://rss-feeds.stations.work';
const slug = 'k3x9qa-amber-river-stone';
const env = { B2_RSS_BUCKET: 'rss-endpoints-private-05802', B2_RSS_KEY_ID: 'id', B2_RSS_KEY: 'key', API_TOKEN: 'api' };
const xml = '<rss><channel><title>Reader</title></channel></rss>';

test('reader host uses the authenticated private origin and generated-feed cache posture', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (url.includes('b2_authorize_account')) return Response.json({
      authorizationToken: 'private-token', apiInfo: { storageApi: { downloadUrl: 'https://download.example' } }
    });
    return new Response(init.method === 'HEAD' ? null : xml, {
      headers: { 'content-type': 'application/rss+xml; charset=utf-8', etag: 'reader-v1', 'x-bz-file-id': 'hidden' }
    });
  };
  try {
    for (const method of ['GET', 'HEAD']) {
      const response = await worker.fetch(new Request(`${base}/${slug}?ignored=yes`, {
        method, headers: { 'if-none-match': 'previous', authorization: 'Bearer client' }
      }), env);
      assert.equal(response.status, 200);
      assert.equal(await response.text(), method === 'HEAD' ? '' : xml);
      assert.equal(response.headers.get('content-type'), 'application/rss+xml; charset=utf-8');
      assert.equal(response.headers.get('cache-control'), 'public, s-maxage=300, max-age=60');
      assert.equal(response.headers.get('etag'), 'reader-v1');
      assert.equal(response.headers.get('x-bz-file-id'), null);
      assert.equal(calls.at(-1).url, `https://download.example/file/${env.B2_RSS_BUCKET}/reader/${slug}`);
      assert.equal(calls.at(-1).init.method, method);
      assert.equal(calls.at(-1).init.headers.Authorization, 'private-token');
      assert.equal(calls.at(-1).init.headers['if-none-match'], 'previous');
    }
    const old = await worker.fetch(new Request(`${base}/reader/existing.xml`), env);
    assert.equal(await old.text(), xml);
    assert.equal(calls.at(-1).url, `https://download.example/file/${env.B2_RSS_BUCKET}/reader/existing.xml`);
    assert.equal(old.headers.get('cache-control'), 'public, max-age=3600');

    globalThis.fetch = async () => new Response('missing', { status: 404 });
    const missing = await worker.fetch(new Request(`${base}/${slug}`), env);
    assert.equal(missing.status, 404);
    assert.equal(missing.headers.get('cache-control'), 'no-store');
  } finally { globalThis.fetch = originalFetch; }
});

test('only a single-segment slug reaches reader/; other paths take the normal routes, never reader/', async () => {
  const originalFetch = globalThis.fetch;
  const fetched = [];
  globalThis.fetch = async (url) => {
    if (String(url).includes('b2_authorize_account')) return Response.json({
      authorizationToken: 't', apiInfo: { storageApi: { downloadUrl: 'https://download.example' } }
    });
    fetched.push(String(url));
    return new Response('object');
  };
  try {
    assert.equal(await (await worker.fetch(new Request(`${base}/healthz`), env)).text(), 'ok');
    assert.equal((await worker.fetch(new Request(`${base}/api/feeds`), env)).status, 401);
    const paths = ['/podcast.xml', '/a/b', '/tts/x', `/audio/${slug}`, '/short', `/${'a'.repeat(81)}`, '/UPPERCASE', '/bad_slug',
      `/${slug}.xml`, `/${slug}/`, '/abcdefgh%2Fother', '/abcdefgh%00'];
    for (const path of paths) assert.equal((await worker.fetch(new Request(base + path), env)).status, 404, path);
    assert.deepEqual(fetched, []);
  } finally { globalThis.fetch = originalFetch; }
});
