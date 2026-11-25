import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';

const base = 'https://rss-feeds.stations.work';
const env = { API_TOKEN: 'api' };

const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

async function admin(path = '/admin') {
  return worker.fetch(new Request(`${base}${path}`), env);
}

test('/admin keeps the API token in memory: it erases the stored copy and never writes one', async () => {
  for (const path of ['/admin', '/admin/']) {
    const res = await admin(path);
    assert.equal(res.status, 200);
    const body = await res.text();
    assert.doesNotMatch(body, /localStorage\.setItem/, `${path} must not store anything in localStorage`);
    assert.doesNotMatch(body, /sessionStorage\.(setItem|getItem)/, `${path} must not keep the token in sessionStorage`);
    assert.doesNotMatch(body, /localStorage\.getItem\("rss-admin-token"\)/, `${path} must not read a stored token back`);
    assert.doesNotMatch(body, /document\.cookie\s*=/, `${path} must not write cookies`);
    assert.match(body, /localStorage\.removeItem\("rss-admin-token"\);/, `${path} must erase the copy earlier page loads left behind`);
  }
});

test('/admin refuses embedding and inline-script escape: CSP and X-Frame-Options', async () => {
  const res = await admin();
  assert.equal(res.headers.get('content-security-policy'), CSP);
  assert.equal(res.headers.get('x-frame-options'), 'DENY');
  assert.match(res.headers.get('content-type') ?? '', /^text\/html/);
});

test('the CSP still allows what the admin page actually does', async () => {
  const body = await (await admin()).text();
  // the page script is inline, so script-src/style-src need 'unsafe-inline' and nothing else
  assert.ok(CSP.includes("script-src 'unsafe-inline'"));
  assert.ok(CSP.includes("style-src 'unsafe-inline'"));
  // every request the page makes is same-origin fetch to /api/*
  for (const match of body.matchAll(/requestJson\("([^"]+)"/g)) {
    assert.ok(match[1].startsWith('/api/'), `admin page fetches ${match[1]}, which connect-src 'self' covers`);
  }
  assert.ok(CSP.includes("connect-src 'self'"));
  // no form submission, no plugin or object sources on the page
  assert.doesNotMatch(body, /<form[\s>]/);
  assert.doesNotMatch(body, /<(object|embed|iframe)[\s>]/);
});

test('/api/* auth and preflight are untouched by the admin hardening', async () => {
  const preflight = await worker.fetch(new Request(`${base}/api/episodes`, { method: 'OPTIONS' }), env);
  assert.equal(preflight.status, 200);
  assert.equal(preflight.headers.get('access-control-allow-origin'), '*');
  assert.equal(preflight.headers.get('access-control-allow-headers'), 'authorization, content-type');
  assert.equal(preflight.headers.get('content-security-policy'), null);
  const denied = await worker.fetch(new Request(`${base}/api/episodes`), env);
  assert.equal(denied.status, 401);
  const allowed = await worker.fetch(new Request(`${base}/api/episodes`, { headers: { Authorization: 'Bearer api' } }), {
    ...env,
    DB: { prepare: () => ({ bind: () => ({ all: async () => ({ results: [] }), first: async () => null, run: async () => ({}) }) }) }
  });
  assert.notEqual(allowed.status, 401);
  assert.equal(allowed.headers.get('content-security-policy'), null);
});
