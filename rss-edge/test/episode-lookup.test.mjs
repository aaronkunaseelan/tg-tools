import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import worker from '../src/index.js';

async function lookup(params, rows = []) {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE episodes (feed TEXT, slug TEXT, title TEXT, description TEXT, pub_date TEXT)');
  for (const slug of rows) db.prepare('INSERT INTO episodes VALUES (?, ?, ?, ?, ?)').run('audio', slug, 'Example', '', '2026-10-03');
  const queries = [];
  const env = { API_TOKEN: 'test', DB: { prepare(sql) {
    queries.push(sql);
    return { bind(...values) { return { async all() {
      return { results: db.prepare(sql).all(...values) };
    } }; } };
  } } };
  try {
    const response = await worker.fetch(new Request('https://example.com/api/episodes?' + new URLSearchParams({ feed: 'audio', ...params }), { headers: { authorization: 'Bearer test' } }), env);
    return { status: response.status, body: await response.json(), queries };
  } finally { db.close(); }
}

test('exact slug lookup bypasses LIKE at every length and binds special characters', async () => {
  for (const slug of ['z'.repeat(48), "2026-10-03-axios-trump's-treasury-ai-0e12c0b1b99d", 'z'.repeat(50), 'z'.repeat(51), 'é_%'.repeat(30)]) {
    const r = await lookup({ slug }, [slug, slug + '-other']);
    assert.equal(r.status, 200);
    assert.deepEqual(r.body.map(row => row.slug), [slug]);
    assert.equal(r.queries.length, 1);
    assert.doesNotMatch(r.queries[0], /LIKE/);
    assert.ok(!r.queries[0].includes(slug));
  }
  assert.deepEqual((await lookup({ slug: 'absent' })).body, []);
});

test('fuzzy search validates the full UTF-8 pattern before reaching D1', async () => {
  assert.equal((await lookup({ search: 'z'.repeat(48) })).status, 200);
  for (const search of ['z'.repeat(49), 'z'.repeat(50), 'z'.repeat(51), 'é'.repeat(25)]) {
    const r = await lookup({ search });
    assert.equal(r.status, 400);
    assert.equal(r.queries.length, 0);
  }
  const r = await lookup({ search: 'Exam' }, ['episode']);
  assert.deepEqual(r.body.map(row => row.slug), ['episode']);
});

test('ambiguous or empty lookup selectors are rejected before D1', async () => {
  for (const params of [{ slug: '', search: '' }, { slug: 'episode', search: 'example' }]) {
    const r = await lookup(params);
    assert.equal(r.status, 400);
    assert.equal(r.queries.length, 0);
  }
});
