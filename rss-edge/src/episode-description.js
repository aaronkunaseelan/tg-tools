// Listener presentation only: retained descriptions and artifacts remain unchanged.
const escape = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');

function safeUrl(value, canonicalize) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(canonicalize(value.trim()));
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function compactSummary(text) {
  // Keep whole sentences, never cut a word/sentence to hit the target.
  const paragraphs = text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  const selected = [];
  let words = 0;
  let chars = 0;
  for (const paragraph of paragraphs) {
    const sentences = [...new Intl.Segmenter('en', { granularity: 'sentence' }).segment(paragraph)]
      .map((part) => part.segment);
    const kept = [];
    for (const sentence of sentences) {
      const value = sentence.trim();
      const count = value.split(/\s+/).length;
      if (words && (words + count > 120 || chars + value.length > 900)) {
        if (kept.length) selected.push(kept.join(' '));
        return selected;
      }
      kept.push(value);
      words += count;
      chars += value.length + 1;
    }
    if (kept.length) selected.push(kept.join(' '));
    if (selected.length === 2) break;
  }
  return selected;
}

export function episodeDescription(episode, canonicalize = (url) => url) {
  const body = String(episode.description || '').trim();
  const footer = new Map();
  const textLines = [];
  for (const line of body.split('\n')) {
    const match = line.match(/^(MP3|Notes|Script|Transcript|Original|Manifest):\s*(https?:\/\/\S+)\s*$/);
    if (match) footer.set(match[1], match[2]);
    else textLines.push(line);
  }
  const candidates = [
    ['Notes', episode.md_url || footer.get('Notes')],
    ['Transcript', episode.speech_text_url || footer.get('Script') || footer.get('Transcript')],
    ['Original', footer.get('Original')],
  ];
  const links = new Map();
  for (const [label, value] of candidates) {
    const url = safeUrl(value, canonicalize);
    if (!url) continue;
    if (links.has(url)) links.get(url).push(label);
    else links.set(url, [label]);
  }
  const hasNotes = [...links.values()].some((labels) => labels.includes('Notes'));
  const linkHtml = [...links].map(([url, labels]) => `<a href="${escape(url)}">${labels.join(' / ')}</a>`).join(' · ');
  const linkText = [...links].map(([url, labels]) => `${labels.join(' / ')}: ${url}`).join('\n');
  // Only compact the known machine-generated plain-text contract. Preserve
  // arbitrary publisher HTML rather than guessing at its semantics.
  const generated = Boolean(episode.speech_text_url && /^Topics: /m.test(body) && !/<[a-z][\s\S]*>/i.test(body));
  if (!generated) {
    const remaining = textLines.join('\n').trim();
    const html = [linkHtml && `<p>${linkHtml}</p>`, remaining].filter(Boolean).join('\n');
    return { html, plain: [linkText, remaining.replace(/<[^>]*>/g, '')].filter(Boolean).join('\n\n'), excerpt: episode.excerpt || '' };
  }
  const remaining = textLines.join('\n');
  const topicMatch = remaining.match(/^Topics: (.+)$/m);
  const summaryText = remaining.split(/^Topics: /m)[0].trim();
  const paragraphs = compactSummary(summaryText);
  const allTopics = topicMatch ? topicMatch[1].replace(/\.$/, '').split(';').map((t) => t.trim()).filter(Boolean) : [];
  const topics = [...new Set(allTopics)].slice(0, 6);
  const published = remaining.match(/^Original published: (.+?)\.(?:\s|$)/m)?.[1];
  const duration = remaining.match(/\bDuration: (\d{2}:\d{2}:\d{2})\./)?.[1];
  const facts = [];
  if (published && published !== 'Unknown') facts.push(`Original published: ${published}.`);
  if (duration) facts.push(`Duration: ${duration}.`);
  facts.push('Machine-generated summary.');
  const html = [
    linkHtml && `<p>${linkHtml}</p>`,
    ...paragraphs.map((p) => `<p>${escape(p)}</p>`),
    topics.length && `<p><strong>Topics</strong></p>\n<ul>${topics.map((t) => `<li>${escape(t)}</li>`).join('')}</ul>`,
    allTopics.length > topics.length && hasNotes && '<p>Full topic list and references in Notes.</p>',
    `<p>${escape(facts.join(' '))}</p>`,
  ].filter(Boolean).join('\n');
  const plain = [linkText, ...paragraphs, topics.length && `Topics\n${topics.map((t) => `• ${t}`).join('\n')}`,
    allTopics.length > topics.length && hasNotes && 'Full topic list and references in Notes.', facts.join(' ')].filter(Boolean).join('\n\n');
  return { html, plain, excerpt: paragraphs.join(' ').split(/\s+/).slice(0, 50).join(' ') };
}
