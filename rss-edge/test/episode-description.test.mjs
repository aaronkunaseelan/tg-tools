import test from 'node:test';
import assert from 'node:assert/strict';
import { episodeDescription } from '../src/episode-description.js';
import worker from '../src/index.js';

const base = 'https://rss-edge.05802.workers.dev';
const notes = `${base}/audio/test.notes.md`;
const script = `${base}/audio/test.script.md`;
const episode = {
  slug: 'test', title: 'Test', pub_date: '2026-10-03', mp3_url: `${base}/audio/test.mp3`, mp3_size: 42,
  md_url: notes, speech_text_url: script, manifest_url: `${base}/audio/test.manifest.json`,
  excerpt: 'Short excerpt', explicit: 'no', block: 'no', keywords: '', updated_at: '1',
  description: `A clear summary of public policy.\n\nA second paragraph.\n\nTopics: Policy; Teaching; Reform; Evidence; Governance; Technology; Future.\n\nOriginal published: Unknown.\n\nMP3: ${base}/audio/test.mp3\n\nNotes: ${notes}\n\nScript: ${script}\n\nOriginal: https://example.com/source?a=1&b=2`,
};

test('top links appear once, raw footer disappears, topics become bounded bullets', () => {
  const result = episodeDescription(episode);
  assert.ok(result.html.startsWith(`<p><a href="${notes}">Notes</a> · <a href="${script}">Transcript</a>`));
  assert.equal(result.html.split(notes).length - 1, 1);
  assert.equal(result.html.split(script).length - 1, 1);
  assert.match(result.html, /source\?a=1&amp;b=2/);
  assert.equal((result.html.match(/<li>/g) || []).length, 6);
  assert.doesNotMatch(result.html, /Unknown|MP3:|Manifest|Topics:|<li>Future/);
  assert.match(result.html, /<p>A clear summary of public policy\.<\/p>/);
  assert.ok(result.plain.startsWith(`Notes: ${notes}\nTranscript: ${script}`));
  assert.match(result.plain, /Topics\n• Policy\n• Teaching/);
});

test('deduplication follows canonical URL while preserving meaningful query and fragment', () => {
  const value = { ...episode, md_url: 'https://rss.stations.work/audio/test.script.md',
    description: episode.description.replace('https://example.com/source?a=1&b=2', `${script}?a=1#original`) };
  const result = episodeDescription(value, (url) => url.replace('https://rss.stations.work', base));
  assert.match(result.html, /Notes \/ Transcript/);
  assert.equal(result.html.split(`href="${script}"`).length - 1, 1);
  assert.match(result.html, /test\.script\.md\?a=1#original/);
});

test('unsafe/missing links are omitted and labels and bodies are escaped', () => {
  const result = episodeDescription({ ...episode, md_url: 'javascript:alert(1)', description: 'Plain text.' });
  assert.doesNotMatch(result.html, /javascript:/);
  assert.match(result.html, /Transcript/);
  const generated = episodeDescription({ ...episode, description: 'A & B are discussed.\n\nTopics: Fish & chips; "Quoted".\n\nOriginal published: 2026-10-01. Duration: 01:02:03.' });
  assert.match(generated.html, /A &amp; B/);
  assert.match(generated.html, /Fish &amp; chips/);
  assert.match(generated.html, /Original published: 2026-10-01/);
  assert.match(generated.html, /Duration: 01:02:03/);
  const empty = episodeDescription({ description: '', excerpt: '' });
  assert.equal(empty.html, '');
});

test('summary preview uses whole sentences; complete retained body is not mutated', () => {
  const sentence = 'This sentence explains the institutional constraints and the practical evidence involved. ';
  const value = { ...episode, description: sentence.repeat(20) + '\n\nTopics: Policy.\n\nOriginal published: Unknown.' };
  const before = value.description;
  const result = episodeDescription(value);
  const paragraph = result.html.match(/<p>(This sentence.+?)<\/p>/)[1];
  assert.ok(paragraph.length <= 900);
  assert.ok(paragraph.split(/\s+/).length <= 120);
  assert.ok(paragraph.endsWith('.'));
  assert.equal(value.description, before);
});

test('sentence segmentation preserves decimals, abbreviations and punctuation', () => {
  const summary = 'Dr. Howell compares version 5.1 with 6.2. "Does it work?" Yes, it does!';
  const result = episodeDescription({ ...episode, description: `${summary}\n\nTopics: Policy.\n\nOriginal published: Unknown.` });
  assert.match(result.html, /Dr\. Howell compares version 5\.1 with 6\.2\./);
  assert.ok(result.plain.includes(summary));
});

test('non-generated publisher HTML is preserved, not summarized or reinterpreted', () => {
  const body = '<p>Publisher <strong>notes</strong>.</p>';
  const result = episodeDescription({ description: body, md_url: notes });
  assert.ok(result.html.endsWith(body));
  assert.doesNotMatch(result.html, /Machine-generated/);
});

test('RSS fields render top links with stable GUID/enclosure and explicit plain fallback', async () => {
  const feed = { id: 'audio', site_url: base, logo_path: 'logo.png', categories: '[]', author: 'Author', email: 'a@example.com', title: 'Title', description: 'Feed', language: 'en', copyright: 'Copyright', explicit: 'no', updated_at: '1' };
  const DB = { prepare: (sql) => ({ bind: () => ({ first: async () => sql.includes('MAX(') ? { count: 1, latest_updated: '1' } : feed, all: async () => ({ results: [episode] }), run: async () => ({}) }) }) };
  const response = await worker.fetch(new Request(`${base}/audio/podcast.xml`), { DB });
  const xml = await response.text();
  assert.match(xml, /<description><!\[CDATA\[<p><a href=/);
  assert.match(xml, /<content:encoded><!\[CDATA\[<p><a href=/);
  assert.match(xml, /<itunes:summary><!\[CDATA\[Notes: https:/);
  assert.match(xml, /<guid isPermaLink="false">test\.md<\/guid>/);
  assert.match(xml, /<enclosure url="https:\/\/rss-edge\.05802\.workers\.dev\/audio\/test\.mp3" length="42"/);
  assert.doesNotMatch(xml, /artifact-links|test\.manifest\.json/);
  assert.match(response.headers.get('cache-control'), /s-maxage=300/);
});
