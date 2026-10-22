import { episodeDescription } from './episode-description.js';

var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// src/helpers.ts
var FEED_IDS = ["audio", "tts"];
function isFeedId(value) {
  return FEED_IDS.includes(value);
}
__name(isFeedId, "isFeedId");
function json(data, init = {}) {
  const headers = new Headers(init.headers);
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("access-control-allow-origin", "*");
  return new Response(JSON.stringify(data, null, 2), { ...init, headers });
}
__name(json, "json");
function html(body, init = {}) {
  const headers = new Headers(init.headers);
  headers.set("content-type", "text/html; charset=utf-8");
  return new Response(body, { ...init, headers });
}
__name(html, "html");
function text(body, init = {}) {
  const headers = new Headers(init.headers);
  headers.set("content-type", "text/plain; charset=utf-8");
  return new Response(body, { ...init, headers });
}
__name(text, "text");
function xmlEscape(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&apos;");
}
__name(xmlEscape, "xmlEscape");
function cdata(value) {
  return `<![CDATA[${value.replaceAll("]]>", "]]]]><![CDATA[>")}]]>`;
}
__name(cdata, "cdata");
function stripHtml(value) {
  return value.replace(/<[^>]+>/g, "");
}
__name(stripHtml, "stripHtml");
function extractExcerpt(body, maxWords = 50) {
  const head = body.split("<!--more-->", 1)[0]?.trim() ?? "";
  const clean = stripHtml(head).replace(/\n+/g, " ").trim();
  const words = clean.split(/\s+/).filter(Boolean);
  if (words.length <= maxWords) {
    return words.join(" ");
  }
  return `${words.slice(0, maxWords).join(" ")}...`;
}
__name(extractExcerpt, "extractExcerpt");
function normalizeYesNo(value, fallback = "no") {
  if (!value) {
    return fallback;
  }
  return value.toLowerCase() === "yes" ? "yes" : "no";
}
__name(normalizeYesNo, "normalizeYesNo");
function parseKeywords(value) {
  if (Array.isArray(value)) {
    return value.map((item) => item.trim()).filter(Boolean).join(",");
  }
  return (value ?? "").trim();
}
__name(parseKeywords, "parseKeywords");
function coerceInt(value, fallback = 0) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.max(0, Math.trunc(value));
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number.parseInt(value, 10);
    if (Number.isFinite(parsed)) {
      return Math.max(0, parsed);
    }
  }
  return fallback;
}
__name(coerceInt, "coerceInt");
function normalizeIsoDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    throw new Error("pub_date must be a valid ISO 8601 date");
  }
  return date.toISOString().replace(".000Z", "Z");
}
__name(normalizeIsoDate, "normalizeIsoDate");
function toRfc822(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.valueOf())) {
    throw new Error(`invalid date: ${String(value)}`);
  }
  return date.toUTCString().replace("GMT", "+0000");
}
__name(toRfc822, "toRfc822");
function parseCategories(raw) {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((item) => typeof item === "string" && item.trim() !== "");
    }
  } catch {
    return [];
  }
  return [];
}
__name(parseCategories, "parseCategories");
function etagFor(parts) {
  return `W/"${parts.map((part) => String(part ?? "")).join(":")}"`;
}
__name(etagFor, "etagFor");

// src/auth.ts
function authorizeApi(request, env) {
  if (!env.API_TOKEN) {
    return json({ error: "API_TOKEN is not configured" }, { status: 500 });
  }
  const header = request.headers.get("authorization");
  if (!header?.startsWith("Bearer ")) {
    return json({ error: "missing bearer token" }, { status: 401 });
  }
  const token = header.slice("Bearer ".length).trim();
  if (!token || token !== env.API_TOKEN) {
    return json({ error: "invalid bearer token" }, { status: 403 });
  }
  return null;
}
__name(authorizeApi, "authorizeApi");
function unauthorizedNewsl() {
  return new Response("Authentication required", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="newsl", charset="UTF-8"',
      "Cache-Control": "no-store"
    }
  });
}
__name(unauthorizedNewsl, "unauthorizedNewsl");
function timingSafeEqual(left, right) {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  const length = Math.max(leftBytes.length, rightBytes.length);
  let difference = leftBytes.length ^ rightBytes.length;
  for (let index = 0; index < length; index++) {
    difference |= (leftBytes[index] ?? 0) ^ (rightBytes[index] ?? 0);
  }
  return difference === 0;
}
__name(timingSafeEqual, "timingSafeEqual");
function authorizeNewsl(request, env) {
  if (!env.NEWSL_RSS_USERNAME || !env.NEWSL_RSS_PASSWORD) {
    return false;
  }
  const supplied = request.headers.get("authorization") ?? "";
  const expected = `Basic ${btoa(`${env.NEWSL_RSS_USERNAME}:${env.NEWSL_RSS_PASSWORD}`)}`;
  return timingSafeEqual(supplied, expected);
}
__name(authorizeNewsl, "authorizeNewsl");

// src/api.ts
var EPISODE_FIELDS = [
  "feed",
  "slug",
  "title",
  "pub_date",
  "description",
  "excerpt",
  "mp3_url",
  "mp3_size",
  "duration",
  "explicit",
  "keywords",
  "block",
  "season",
  "episode_num",
  "episode_type",
  "route",
  "episode_kind",
  "md_url",
  "speech_text_url",
  "manifest_url",
  "source_md_url"
];
async function readJsonBody(request) {
  try {
    return await request.json();
  } catch {
    throw new Error("request body must be valid JSON");
  }
}
__name(readJsonBody, "readJsonBody");
function asString(value) {
  return typeof value === "string" ? value : void 0;
}
__name(asString, "asString");
function normalizeNullableString(value) {
  if (value === null) {
    return null;
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed === "" ? null : trimmed;
  }
  return void 0;
}
__name(normalizeNullableString, "normalizeNullableString");
function normalizeNullableAbsoluteUrl(value, field) {
  const normalized = normalizeNullableString(value);
  if (normalized === void 0 || normalized === null) {
    return normalized;
  }
  if (!normalized.startsWith("http://") && !normalized.startsWith("https://")) {
    throw new Error(`${field} must be absolute`);
  }
  return normalized;
}
__name(normalizeNullableAbsoluteUrl, "normalizeNullableAbsoluteUrl");
function normalizeEpisodeInput(input, partial = false) {
  const result = {};
  const feed = asString(input.feed);
  if (feed !== void 0) {
    if (!isFeedId(feed)) {
      throw new Error("feed must be 'audio' or 'tts'");
    }
    result.feed = feed;
  } else if (!partial) {
    throw new Error("feed is required");
  }
  const slug = asString(input.slug)?.trim();
  if (slug !== void 0) {
    if (!slug) {
      throw new Error("slug cannot be empty");
    }
    result.slug = slug;
  } else if (!partial) {
    throw new Error("slug is required");
  }
  const title = asString(input.title)?.trim();
  if (title !== void 0) {
    if (!title) {
      throw new Error("title cannot be empty");
    }
    result.title = title;
  } else if (!partial) {
    throw new Error("title is required");
  }
  const pubDate = asString(input.pub_date);
  if (pubDate !== void 0) {
    result.pub_date = normalizeIsoDate(pubDate);
  } else if (!partial) {
    throw new Error("pub_date is required");
  }
  const description = asString(input.description);
  if (description !== void 0) {
    result.description = description;
  } else if (!partial) {
    result.description = "";
  }
  const excerpt = asString(input.excerpt);
  if (excerpt !== void 0) {
    result.excerpt = excerpt;
  } else if (description !== void 0) {
    result.excerpt = extractExcerpt(description);
  } else if (!partial) {
    result.excerpt = "";
  }
  const mp3Url = asString(input.mp3_url)?.trim();
  if (mp3Url !== void 0) {
    if (!mp3Url.startsWith("http://") && !mp3Url.startsWith("https://")) {
      throw new Error("mp3_url must be absolute");
    }
    result.mp3_url = mp3Url;
  } else if (!partial) {
    throw new Error("mp3_url is required");
  }
  if (input.mp3_size !== void 0) {
    result.mp3_size = coerceInt(input.mp3_size);
  } else if (!partial) {
    result.mp3_size = 0;
  }
  if (input.duration !== void 0) {
    result.duration = input.duration === null ? null : asString(input.duration) ?? null;
  } else if (!partial) {
    result.duration = null;
  }
  if (input.explicit !== void 0 || !partial) {
    result.explicit = normalizeYesNo(asString(input.explicit), "no");
  }
  if (input.block !== void 0 || !partial) {
    result.block = normalizeYesNo(asString(input.block), "no");
  }
  if (input.keywords !== void 0) {
    result.keywords = parseKeywords(input.keywords);
  } else if (!partial) {
    result.keywords = "";
  }
  if (input.season !== void 0) {
    result.season = asString(input.season)?.trim() ?? "";
  } else if (!partial) {
    result.season = "";
  }
  if (input.episode_num !== void 0) {
    result.episode_num = asString(input.episode_num)?.trim() ?? "";
  } else if (!partial) {
    result.episode_num = "";
  }
  if (input.episode_type !== void 0) {
    result.episode_type = asString(input.episode_type)?.trim() ?? "";
  } else if (!partial) {
    result.episode_type = "";
  }
  for (const field of ["route", "episode_kind"]) {
    if (input[field] !== void 0) {
      result[field] = normalizeNullableString(input[field]) ?? null;
    } else if (!partial) {
      result[field] = null;
    }
  }
  for (const field of ["md_url", "speech_text_url", "manifest_url", "source_md_url"]) {
    if (input[field] !== void 0) {
      result[field] = normalizeNullableAbsoluteUrl(input[field], field) ?? null;
    } else if (!partial) {
      result[field] = null;
    }
  }
  return result;
}
__name(normalizeEpisodeInput, "normalizeEpisodeInput");
async function listEpisodes(request, env) {
  const url = new URL(request.url);
  const feed = url.searchParams.get("feed");
  const search = url.searchParams.get("search")?.trim();
  let sql = "SELECT * FROM episodes";
  const clauses = [];
  const values = [];
  if (feed) {
    if (!isFeedId(feed)) {
      return json({ error: "invalid feed" }, { status: 400 });
    }
    clauses.push(`feed = ?${values.length + 1}`);
    values.push(feed);
  }
  const slug = url.searchParams.get("slug");
  if (slug !== null && search) {
    return json({ error: "use either slug or search" }, { status: 400 });
  }
  if (slug !== null) {
    if (!slug.trim()) {
      return json({ error: "slug must not be empty" }, { status: 400 });
    }
    clauses.push(`slug = ?${values.length + 1}`);
    values.push(slug);
  } else if (search) {
    const needle = `%${search}%`;
    // D1's LIKE limit applies to the complete UTF-8 pattern, including %.
    if (new TextEncoder().encode(needle).length > 50) {
      return json({ error: "search pattern exceeds 50 bytes; use slug for exact lookup" }, { status: 400 });
    }
    clauses.push(`(title LIKE ?${values.length + 1} OR slug LIKE ?${values.length + 2} OR description LIKE ?${values.length + 3})`);
    values.push(needle, needle, needle);
  }
  if (clauses.length > 0) {
    sql += ` WHERE ${clauses.join(" AND ")}`;
  }
  sql += " ORDER BY pub_date DESC";
  const rows = await env.DB.prepare(sql).bind(...values).all();
  return json(rows.results ?? []);
}
__name(listEpisodes, "listEpisodes");
async function getEpisode(id, env) {
  const row = await env.DB.prepare("SELECT * FROM episodes WHERE id = ?1").bind(id).first();
  if (!row) {
    return json({ error: "episode not found" }, { status: 404 });
  }
  return json(row);
}
__name(getEpisode, "getEpisode");
async function createEpisode(request, env) {
  let input;
  try {
    input = normalizeEpisodeInput(await readJsonBody(request), false);
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : "invalid request" }, { status: 400 });
  }
  const fields = EPISODE_FIELDS;
  const placeholders = fields.map((_, index) => `?${index + 1}`).join(", ");
  const sql = `
    INSERT INTO episodes (${fields.join(", ")})
    VALUES (${placeholders})
    RETURNING *;
  `;
  try {
    const row = await env.DB.prepare(sql).bind(...fields.map((field) => input[field])).first();
    return json(row, { status: 201 });
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : "insert failed" }, { status: 400 });
  }
}
__name(createEpisode, "createEpisode");
async function updateEpisode(id, request, env) {
  let input;
  try {
    input = normalizeEpisodeInput(await readJsonBody(request), true);
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : "invalid request" }, { status: 400 });
  }
  const assignments = EPISODE_FIELDS.filter((field) => input[field] !== void 0);
  if (assignments.length === 0) {
    return json({ error: "no fields to update" }, { status: 400 });
  }
  const values = assignments.map((field) => input[field]);
  const sql = `
    UPDATE episodes
    SET ${assignments.map((field, index) => `${field} = ?${index + 1}`).join(", ")},
        updated_at = datetime('now')
    WHERE id = ?${assignments.length + 1}
    RETURNING *;
  `;
  const row = await env.DB.prepare(sql).bind(...values, id).first();
  if (!row) {
    return json({ error: "episode not found" }, { status: 404 });
  }
  return json(row);
}
__name(updateEpisode, "updateEpisode");
async function deleteEpisode(id, env) {
  const row = await env.DB.prepare("DELETE FROM episodes WHERE id = ?1 RETURNING id, slug, feed").bind(id).first();
  if (!row) {
    return json({ error: "episode not found" }, { status: 404 });
  }
  return json({ ok: true, deleted: row });
}
__name(deleteEpisode, "deleteEpisode");
async function listFeeds(env) {
  const rows = await env.DB.prepare("SELECT * FROM feeds ORDER BY id ASC").all();
  return json(rows.results ?? []);
}
__name(listFeeds, "listFeeds");
async function updateFeed(id, request, env) {
  if (!isFeedId(id)) {
    return json({ error: "feed not found" }, { status: 404 });
  }
  const body = await readJsonBody(request);
  const updatable = [
    "title",
    "description",
    "author",
    "email",
    "language",
    "copyright",
    "logo_path",
    "explicit",
    "site_url",
    "feed_url",
    "categories"
  ];
  const values = [];
  const assignments = [];
  for (const key of updatable) {
    if (!(key in body)) {
      continue;
    }
    let value = body[key];
    if (key === "explicit") {
      value = normalizeYesNo(asString(value), "no");
    }
    if (key === "categories") {
      value = typeof value === "string" ? value : JSON.stringify(value ?? []);
    }
    if (typeof value !== "string") {
      return json({ error: `${key} must be a string` }, { status: 400 });
    }
    assignments.push(`${key} = ?${values.length + 1}`);
    values.push(value);
  }
  if (assignments.length === 0) {
    return json({ error: "no feed fields to update" }, { status: 400 });
  }
  const sql = `
    UPDATE feeds
    SET ${assignments.join(", ")},
        updated_at = datetime('now')
    WHERE id = ?${values.length + 1}
    RETURNING *;
  `;
  const row = await env.DB.prepare(sql).bind(...values, id).first();
  if (!row) {
    return json({ error: "feed not found" }, { status: 404 });
  }
  return json(row);
}
__name(updateFeed, "updateFeed");

// src/admin.ts
function renderAdmin() {
  return html(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>RSS Admin</title>
    <style>
      :root {
        --bg: #f4f0e8;
        --paper: #fffdf8;
        --ink: #18211f;
        --muted: #6a746f;
        --accent: #18654b;
        --accent-2: #d98c2f;
        --line: #d7d0c4;
        --danger: #9f2b2b;
        --shadow: 0 18px 40px rgba(24, 33, 31, 0.12);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: Georgia, "Iowan Old Style", serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(217, 140, 47, 0.12), transparent 28rem),
          linear-gradient(160deg, #f8f4ea 0%, #ece7dd 100%);
      }
      .page {
        max-width: 1440px;
        margin: 0 auto;
        padding: 1.25rem;
      }
      .hero {
        background: var(--paper);
        border: 1px solid var(--line);
        border-radius: 24px;
        box-shadow: var(--shadow);
        padding: 1.25rem;
        margin-bottom: 1rem;
      }
      .hero h1 {
        margin: 0 0 0.25rem;
        font-size: clamp(2rem, 4vw, 3.5rem);
        line-height: 0.95;
      }
      .hero p {
        margin: 0;
        color: var(--muted);
        max-width: 52rem;
      }
      .toolbar {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 0.75rem;
        margin-top: 1rem;
      }
      .card {
        background: var(--paper);
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 1rem;
        box-shadow: var(--shadow);
      }
      .layout {
        display: grid;
        grid-template-columns: minmax(0, 1.4fr) minmax(320px, 0.8fr);
        gap: 1rem;
      }
      .stack {
        display: grid;
        gap: 1rem;
      }
      label {
        display: grid;
        gap: 0.35rem;
        font-size: 0.9rem;
        color: var(--muted);
      }
      input, select, textarea, button {
        font: inherit;
      }
      input, select, textarea {
        width: 100%;
        padding: 0.7rem 0.85rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: #fff;
        color: var(--ink);
      }
      textarea {
        min-height: 9rem;
        resize: vertical;
      }
      button {
        border: none;
        border-radius: 999px;
        padding: 0.75rem 1rem;
        cursor: pointer;
        background: var(--accent);
        color: #fff;
      }
      button.secondary {
        background: #e8e1d5;
        color: var(--ink);
      }
      button.danger {
        background: var(--danger);
      }
      .button-row {
        display: flex;
        gap: 0.65rem;
        flex-wrap: wrap;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th, td {
        text-align: left;
        padding: 0.7rem 0.5rem;
        border-bottom: 1px solid var(--line);
        vertical-align: top;
      }
      th {
        color: var(--muted);
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      tr.active {
        background: rgba(24, 101, 75, 0.08);
      }
      .pill {
        display: inline-block;
        padding: 0.2rem 0.5rem;
        border-radius: 999px;
        background: #efe7d8;
        font-size: 0.78rem;
      }
      .meta {
        color: var(--muted);
        font-size: 0.9rem;
      }
      #status {
        min-height: 1.25rem;
        color: var(--accent);
      }
      #status.error {
        color: var(--danger);
      }
      @media (max-width: 980px) {
        .layout { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <main class="page">
      <section class="hero">
        <h1>RSS Admin</h1>
        <p>Edit episode metadata in D1, preview the live XML, and keep the B2 object store limited to audio and artwork.</p>
        <div class="toolbar">
          <label>Bearer token
            <input id="token" type="password" placeholder="Paste API token" />
          </label>
          <label>Feed
            <select id="feedFilter">
              <option value="audio">audio</option>
            </select>
          </label>
          <label>Search
            <input id="search" type="search" placeholder="title, slug, description" />
          </label>
          <div class="button-row" style="align-items:end;">
            <button id="refreshBtn" type="button">Refresh</button>
            <button id="previewBtn" type="button" class="secondary">Preview XML</button>
          </div>
        </div>
      </section>

      <div class="layout">
        <section class="stack">
          <article class="card">
            <div class="button-row" style="justify-content: space-between; margin-bottom: 0.75rem;">
              <strong>Episodes</strong>
              <button id="newEpisodeBtn" type="button" class="secondary">New episode</button>
            </div>
            <div id="status" class="meta"></div>
            <div style="overflow:auto;">
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Title</th>
                    <th>Feed</th>
                    <th>Size</th>
                  </tr>
                </thead>
                <tbody id="episodesBody"></tbody>
              </table>
            </div>
          </article>

          <article class="card">
            <div class="button-row" style="justify-content: space-between; margin-bottom: 0.75rem;">
              <strong>Feed config</strong>
              <button id="saveFeedBtn" type="button">Save feed</button>
            </div>
            <div class="stack">
              <label>Title <input id="feed_title" /></label>
              <label>Description <textarea id="feed_description"></textarea></label>
              <label>Author <input id="feed_author" /></label>
              <label>Email <input id="feed_email" /></label>
              <label>Language <input id="feed_language" /></label>
              <label>Copyright <input id="feed_copyright" /></label>
              <label>Logo path <input id="feed_logo_path" /></label>
              <label>Explicit
                <select id="feed_explicit">
                  <option value="no">no</option>
                  <option value="yes">yes</option>
                </select>
              </label>
              <label>Site URL <input id="feed_site_url" /></label>
              <label>Feed URL <input id="feed_feed_url" /></label>
              <label>Categories JSON <textarea id="feed_categories"></textarea></label>
            </div>
          </article>
        </section>

        <aside class="stack">
          <article class="card">
            <div class="button-row" style="justify-content: space-between; margin-bottom: 0.75rem;">
              <strong id="editorTitle">Episode editor</strong>
              <span class="pill" id="editorMode">create</span>
            </div>
            <div class="stack">
              <label>Feed
                <select id="ep_feed">
                  <option value="audio">audio</option>
                </select>
              </label>
              <label>Slug <input id="ep_slug" placeholder="2026-03-22-slug" /></label>
              <label>Title <input id="ep_title" /></label>
              <label>Pub date <input id="ep_pub_date" placeholder="2026-03-22T12:00:00Z" /></label>
              <label>MP3 URL <input id="ep_mp3_url" /></label>
              <label>MP3 size (bytes) <input id="ep_mp3_size" type="number" min="0" step="1" /></label>
              <label>Duration <input id="ep_duration" placeholder="HH:MM:SS" /></label>
              <label>Keywords <input id="ep_keywords" placeholder="comma,separated" /></label>
              <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
                <label>Explicit
                  <select id="ep_explicit">
                    <option value="no">no</option>
                    <option value="yes">yes</option>
                  </select>
                </label>
                <label>Block
                  <select id="ep_block">
                    <option value="no">no</option>
                    <option value="yes">yes</option>
                  </select>
                </label>
              </div>
              <div style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.75rem;">
                <label>Season <input id="ep_season" /></label>
                <label>Episode # <input id="ep_episode_num" /></label>
                <label>Episode type <input id="ep_episode_type" /></label>
              </div>
              <label>Excerpt <textarea id="ep_excerpt"></textarea></label>
              <label>Description <textarea id="ep_description" style="min-height: 14rem;"></textarea></label>
            </div>
            <div class="button-row" style="margin-top: 0.9rem;">
              <button id="saveEpisodeBtn" type="button">Save episode</button>
              <button id="deleteEpisodeBtn" type="button" class="danger">Delete</button>
              <button id="resetEpisodeBtn" type="button" class="secondary">Reset</button>
            </div>
          </article>
        </aside>
      </div>
    </main>

    <script>
      const state = {
        token: localStorage.getItem("rss-admin-token") || "",
        episodes: [],
        feeds: [],
        selectedEpisodeId: null,
      };

      const ids = [
        "token","feedFilter","search","episodesBody","status","editorTitle","editorMode",
        "ep_feed","ep_slug","ep_title","ep_pub_date","ep_mp3_url","ep_mp3_size","ep_duration",
        "ep_keywords","ep_explicit","ep_block","ep_season","ep_episode_num","ep_episode_type",
        "ep_excerpt","ep_description","feed_title","feed_description","feed_author","feed_email",
        "feed_language","feed_copyright","feed_logo_path","feed_explicit","feed_site_url",
        "feed_feed_url","feed_categories"
      ];
      const el = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
      el.token.value = state.token;

      function setStatus(message, isError = false) {
        el.status.textContent = message;
        el.status.classList.toggle("error", isError);
      }

      function authHeaders() {
        const token = el.token.value.trim();
        if (!token) {
          throw new Error("API token is required");
        }
        localStorage.setItem("rss-admin-token", token);
        return {
          "Authorization": "Bearer " + token,
          "Content-Type": "application/json"
        };
      }

      async function requestJson(url, options = {}) {
        const response = await fetch(url, {
          ...options,
          headers: {
            ...(options.headers || {}),
            ...authHeaders()
          }
        });
        const text = await response.text();
        const body = text ? JSON.parse(text) : null;
        if (!response.ok) {
          throw new Error(body && body.error ? body.error : "Request failed");
        }
        return body;
      }

      function bytes(value) {
        const num = Number(value || 0);
        if (!Number.isFinite(num) || num <= 0) {
          return "0";
        }
        const units = ["B", "KB", "MB", "GB"];
        let index = 0;
        let current = num;
        while (current >= 1024 && index < units.length - 1) {
          current /= 1024;
          index += 1;
        }
        return current.toFixed(current >= 10 || index === 0 ? 0 : 1) + " " + units[index];
      }

      function formEpisode() {
        return {
          feed: el.ep_feed.value,
          slug: el.ep_slug.value.trim(),
          title: el.ep_title.value.trim(),
          pub_date: el.ep_pub_date.value.trim(),
          mp3_url: el.ep_mp3_url.value.trim(),
          mp3_size: Number(el.ep_mp3_size.value || 0),
          duration: el.ep_duration.value.trim() || null,
          keywords: el.ep_keywords.value.trim(),
          explicit: el.ep_explicit.value,
          block: el.ep_block.value,
          season: el.ep_season.value.trim(),
          episode_num: el.ep_episode_num.value.trim(),
          episode_type: el.ep_episode_type.value.trim(),
          excerpt: el.ep_excerpt.value,
          description: el.ep_description.value,
        };
      }

      function fillEpisode(episode) {
        state.selectedEpisodeId = episode && episode.id || null;
        el.editorMode.textContent = state.selectedEpisodeId ? "edit" : "create";
        el.editorTitle.textContent = state.selectedEpisodeId ? "Episode editor" : "New episode";
        el.ep_feed.value = episode?.feed || el.feedFilter.value || "audio";
        el.ep_slug.value = episode?.slug || "";
        el.ep_title.value = episode?.title || "";
        el.ep_pub_date.value = episode?.pub_date || "";
        el.ep_mp3_url.value = episode?.mp3_url || "";
        el.ep_mp3_size.value = episode?.mp3_size || 0;
        el.ep_duration.value = episode?.duration || "";
        el.ep_keywords.value = episode?.keywords || "";
        el.ep_explicit.value = episode?.explicit || "no";
        el.ep_block.value = episode?.block || "no";
        el.ep_season.value = episode?.season || "";
        el.ep_episode_num.value = episode?.episode_num || "";
        el.ep_episode_type.value = episode?.episode_type || "";
        el.ep_excerpt.value = episode?.excerpt || "";
        el.ep_description.value = episode?.description || "";
        renderEpisodes();
      }

      function renderEpisodes() {
        el.episodesBody.innerHTML = "";
        for (const episode of state.episodes) {
          const row = document.createElement("tr");
          if (episode.id === state.selectedEpisodeId) {
            row.classList.add("active");
          }
          row.innerHTML = [
            "<td>" + episode.pub_date.slice(0, 10) + "</td>",
            "<td><strong>" + episode.title.replaceAll("<", "&lt;") + "</strong><div class='meta'>" + episode.slug + "</div></td>",
            "<td><span class='pill'>" + episode.feed + "</span></td>",
            "<td>" + bytes(episode.mp3_size) + "</td>"
          ].join("");
          row.addEventListener("click", () => fillEpisode(episode));
          el.episodesBody.appendChild(row);
        }
      }

      function renderFeedConfig() {
        const feed = state.feeds.find((item) => item.id === el.feedFilter.value);
        if (!feed) {
          return;
        }
        el.feed_title.value = feed.title || "";
        el.feed_description.value = feed.description || "";
        el.feed_author.value = feed.author || "";
        el.feed_email.value = feed.email || "";
        el.feed_language.value = feed.language || "";
        el.feed_copyright.value = feed.copyright || "";
        el.feed_logo_path.value = feed.logo_path || "";
        el.feed_explicit.value = feed.explicit || "no";
        el.feed_site_url.value = feed.site_url || "";
        el.feed_feed_url.value = feed.feed_url || "";
        el.feed_categories.value = feed.categories || "[]";
      }

      async function loadEpisodes() {
        const params = new URLSearchParams();
        if (el.feedFilter.value) params.set("feed", el.feedFilter.value);
        if (el.search.value.trim()) params.set("search", el.search.value.trim());
        state.episodes = await requestJson("/api/episodes?" + params.toString());
        renderEpisodes();
        if (state.selectedEpisodeId) {
          const current = state.episodes.find((episode) => episode.id === state.selectedEpisodeId);
          if (current) fillEpisode(current);
        }
      }

      async function loadFeeds() {
        state.feeds = await requestJson("/api/feeds");
        renderFeedConfig();
      }

      async function refreshAll() {
        setStatus("Loading...");
        try {
          await Promise.all([loadEpisodes(), loadFeeds()]);
          setStatus("Loaded " + state.episodes.length + " episodes.");
        } catch (error) {
          setStatus(error.message, true);
        }
      }

      document.getElementById("refreshBtn").addEventListener("click", refreshAll);
      document.getElementById("newEpisodeBtn").addEventListener("click", () => fillEpisode(null));
      document.getElementById("resetEpisodeBtn").addEventListener("click", () => fillEpisode(null));
      document.getElementById("feedFilter").addEventListener("change", async () => {
        fillEpisode(null);
        renderFeedConfig();
        await refreshAll();
      });
      document.getElementById("previewBtn").addEventListener("click", () => {
        window.open("/audio/podcast.xml", "_blank");
      });
      document.getElementById("search").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          refreshAll();
        }
      });

      document.getElementById("saveEpisodeBtn").addEventListener("click", async () => {
        try {
          const payload = formEpisode();
          const url = state.selectedEpisodeId ? "/api/episodes/" + state.selectedEpisodeId : "/api/episodes";
          const method = state.selectedEpisodeId ? "PUT" : "POST";
          const saved = await requestJson(url, { method, body: JSON.stringify(payload) });
          fillEpisode(saved);
          await refreshAll();
          setStatus("Episode saved.");
        } catch (error) {
          setStatus(error.message, true);
        }
      });

      document.getElementById("deleteEpisodeBtn").addEventListener("click", async () => {
        if (!state.selectedEpisodeId) {
          setStatus("Select an episode first.", true);
          return;
        }
        if (!window.confirm("Delete this D1 row? The MP3 stays in B2.")) {
          return;
        }
        try {
          await requestJson("/api/episodes/" + state.selectedEpisodeId, { method: "DELETE" });
          fillEpisode(null);
          await refreshAll();
          setStatus("Episode deleted.");
        } catch (error) {
          setStatus(error.message, true);
        }
      });

      document.getElementById("saveFeedBtn").addEventListener("click", async () => {
        try {
          const payload = {
            title: el.feed_title.value,
            description: el.feed_description.value,
            author: el.feed_author.value,
            email: el.feed_email.value,
            language: el.feed_language.value,
            copyright: el.feed_copyright.value,
            logo_path: el.feed_logo_path.value,
            explicit: el.feed_explicit.value,
            site_url: el.feed_site_url.value,
            feed_url: el.feed_feed_url.value,
            categories: el.feed_categories.value
          };
          await requestJson("/api/feeds/" + el.feedFilter.value, {
            method: "PUT",
            body: JSON.stringify(payload)
          });
          await loadFeeds();
          setStatus("Feed config saved.");
        } catch (error) {
          setStatus(error.message, true);
        }
      });

      if (state.token) {
        refreshAll();
      } else {
        fillEpisode(null);
      }
    <\/script>
  </body>
</html>`);
}
__name(renderAdmin, "renderAdmin");

// src/feed.ts
function categoriesXml(categories) {
  return categories.map((category) => {
    const parts = category.split(" > ").map((part) => part.trim()).filter(Boolean);
    if (parts.length === 2) {
      return [
        `        <itunes:category text="${xmlEscape(parts[0])}">`,
        `            <itunes:category text="${xmlEscape(parts[1])}" />`,
        "        </itunes:category>"
      ].join("\n");
    }
    return `        <itunes:category text="${xmlEscape(category)}" />`;
  }).join("\n");
}
__name(categoriesXml, "categoriesXml");
function episodeKeywords(episode) {
  const keywords = episode.keywords ?? "";
  const additions = [episode.route ?? "", episode.episode_kind ?? ""].map((value) => value.trim()).filter(Boolean);
  if (additions.length === 0) {
    return keywords;
  }
  return [keywords, ...additions].filter(Boolean).join(",");
}
__name(episodeKeywords, "episodeKeywords");
const PUBLIC_BASE = "https://rss-edge.05802.workers.dev";
const LEGACY_HOSTS = new Set(["rss-b2-proxy.aaronkuna.workers.dev", "rss.stations.work", "rss-b2-proxy.05802.workers.dev"]);
function publicUrl(value) {
  if (!value) return value;
  try {
    const url = new URL(value);
    if (LEGACY_HOSTS.has(url.hostname) || url.hostname === "rss-edge.05802.workers.dev") {
      return `${PUBLIC_BASE}${url.pathname}${url.search}${url.hash}`;
    }
  } catch { /* Preserve non-URL metadata. */ }
  return value;
}
function generateXml(feed, episodes) {
  const now = toRfc822(/* @__PURE__ */ new Date());
  const siteUrl = publicUrl(feed.site_url);
  const logoUrl = `${siteUrl.replace(/\/$/, "")}/${feed.logo_path}`;
  const categories = categoriesXml(parseCategories(feed.categories));
  // Do not publish an enclosure pointing outside the sole delivery hostname.
  // The external pscrb.fm episode remains in D1 until its media can be licensed/imported.
  const items = episodes.filter((episode) => {
    try { return new URL(publicUrl(episode.mp3_url)).origin === PUBLIC_BASE; }
    catch { return false; }
  }).map((episode) => {
    const keywords = episodeKeywords(episode);
    const presentation = episodeDescription(episode, publicUrl);
    const summary = presentation.plain;
    const content = presentation.html;
    const link = publicUrl(episode.md_url || episode.mp3_url);
    return `        <item>
            <title>${xmlEscape(episode.title)}</title>
            <itunes:title>${xmlEscape(episode.title)}</itunes:title>
            <description>${cdata(content)}</description>
            <content:encoded>${cdata(content)}</content:encoded>
            <link>${xmlEscape(link)}</link>
            <pubDate>${toRfc822(episode.pub_date)}</pubDate>
            <guid isPermaLink="false">${xmlEscape(`${episode.slug}.md`)}</guid>
            <enclosure url="${xmlEscape(publicUrl(episode.mp3_url))}" length="${episode.mp3_size}" type="audio/mpeg"/>
            <itunes:author>${xmlEscape(feed.author)}</itunes:author>
            <itunes:summary>${cdata(summary)}</itunes:summary>
            <itunes:subtitle>${cdata(episode.excerpt)}</itunes:subtitle>
            <itunes:keywords>${xmlEscape(keywords)}</itunes:keywords>
            <itunes:season>${xmlEscape(episode.season ?? "")}</itunes:season>
            <itunes:episode>${xmlEscape(episode.episode_num ?? "")}</itunes:episode>
            <itunes:episodeType>${xmlEscape(episode.episode_type ?? "")}</itunes:episodeType>
            <itunes:image href="${xmlEscape(logoUrl)}" />
            <itunes:explicit>${xmlEscape(episode.explicit)}</itunes:explicit>
            <itunes:block>${xmlEscape(episode.block)}</itunes:block>
        </item>`;
  }).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:atom="http://www.w3.org/2005/Atom"
    xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
    xml:lang="${xmlEscape(feed.language)}">
    <channel>
        <atom:link href="${xmlEscape(`${PUBLIC_BASE}/${feed.id}/podcast.xml`)}" rel="self" type="application/rss+xml" />
        <title>${xmlEscape(feed.title)}</title>
        <description>${cdata(feed.description)}</description>
        <link>${xmlEscape(siteUrl)}</link>
        <language>${xmlEscape(feed.language)}</language>
        <managingEditor>${xmlEscape(feed.email)} (${xmlEscape(feed.author)})</managingEditor>
        <webMaster>${xmlEscape(feed.email)} (${xmlEscape(feed.author)})</webMaster>
        <copyright>${xmlEscape(feed.copyright)}</copyright>
        <pubDate>${now}</pubDate>
        <lastBuildDate>${now}</lastBuildDate>
        <image>
            <link>${xmlEscape(siteUrl)}</link>
            <url>${xmlEscape(logoUrl)}</url>
            <title>${xmlEscape(feed.title)}</title>
        </image>
        <itunes:subtitle>${cdata(feed.description)}</itunes:subtitle>
        <itunes:author>${xmlEscape(feed.author)}</itunes:author>
        <itunes:summary>${cdata(feed.description)}</itunes:summary>
        <itunes:owner>
            <itunes:name>${xmlEscape(feed.author)}</itunes:name>
            <itunes:email>${xmlEscape(feed.email)}</itunes:email>
        </itunes:owner>
        <itunes:image href="${xmlEscape(logoUrl)}" />
${categories}
        <itunes:type>episodic</itunes:type>
        <itunes:explicit>${xmlEscape(feed.explicit)}</itunes:explicit>
${items}
    </channel>
</rss>
`.replace(/https?:\/\/(?:rss\.stations\.work|rss-b2-proxy\.aaronkuna\.workers\.dev|rss-b2-proxy\.05802\.workers\.dev)/g, PUBLIC_BASE);
}
__name(generateXml, "generateXml");
// Feeds are rendered once per change and stored gzip-compressed in feed_gz; a GET returns the
// stored bytes (one small row read instead of every episode row and a 20+ ms render; the XML
// itself is ~5x larger to decode). API writes drop the stored copies. A copy older than a day, or from another presentation version, is rebuilt,
// which also picks up D1 edits made outside the API.
var FEED_PRESENTATION = "listener-description-v2";
var FEED_STORE_MAX_AGE_S = 86400;
function toBase64(bytes) {
  if (bytes.toBase64) return bytes.toBase64();
  let binary = "";
  for (let i = 0; i < bytes.length; i += 32768) binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  return btoa(binary);
}
function fromBase64(text) {
  return Uint8Array.fromBase64 ? Uint8Array.fromBase64(text) : Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
}
async function gzip(text) {
  return new Uint8Array(await new Response(new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer());
}
async function storedFeed(env, feedId) {
  const row = await env.DB.prepare("SELECT version, etag, gz, built_at FROM feed_gz WHERE feed = ?1").bind(feedId).first();
  const now = Math.floor(Date.now() / 1e3);
  if (row && row.version === FEED_PRESENTATION && now - row.built_at < FEED_STORE_MAX_AGE_S) {
    return row;
  }
  const [feed, meta] = await Promise.all([
    env.DB.prepare("SELECT * FROM feeds WHERE id = ?1").bind(feedId).first(),
    env.DB.prepare(
      "SELECT MAX(updated_at) AS latest_updated, COUNT(*) AS count FROM episodes WHERE feed = ?1 AND block != 'yes'"
    ).bind(feedId).first()
  ]);
  if (!feed) {
    return null;
  }
  const etag = etagFor([
    "workers-dev-private-v1", FEED_PRESENTATION, feedId,
    feed.updated_at,
    meta?.latest_updated,
    meta?.count ?? 0
  ]);
  // Unchanged sources keep the stored body (and its build date); only the age resets.
  let gz = row && row.version === FEED_PRESENTATION && row.etag === etag ? row.gz : null;
  if (gz === null) {
    const episodes = await env.DB.prepare(
      "SELECT * FROM episodes WHERE feed = ?1 AND block != 'yes' ORDER BY pub_date DESC"
    ).bind(feedId).all();
    gz = toBase64(await gzip(generateXml(feed, episodes.results ?? [])));
  }
  await env.DB.prepare("INSERT OR REPLACE INTO feed_gz (feed, version, etag, gz, built_at) VALUES (?1, ?2, ?3, ?4, ?5)")
    .bind(feedId, FEED_PRESENTATION, etag, gz, now).run();
  return { etag, gz };
}
__name(storedFeed, "storedFeed");
async function generateFeedResponse(request, env, feedId) {
  const stored = await storedFeed(env, feedId);
  if (!stored) {
    return new Response("Feed not found", { status: 404 });
  }
  if (request.headers.get("if-none-match") === stored.etag) {
    return new Response(null, {
      status: 304,
      headers: {
        ETag: stored.etag,
        "Cache-Control": "public, s-maxage=300, max-age=60"
      }
    });
  }
  const headers = {
    "Content-Type": "application/rss+xml; charset=utf-8",
    "Cache-Control": "public, s-maxage=300, max-age=60",
    ETag: stored.etag,
    Vary: "Accept-Encoding",
    "Access-Control-Allow-Origin": "*"
  };
  const bytes = fromBase64(stored.gz);
  if (/\bgzip\b/.test(request.headers.get("accept-encoding") || "")) {
    return new Response(bytes, { status: 200, encodeBody: "manual", headers: { ...headers, "Content-Encoding": "gzip" } });
  }
  return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip")), { status: 200, headers });
}
__name(generateFeedResponse, "generateFeedResponse");

// src/proxy.ts
// Public RSS storage must never be fetched anonymously. Newsl uses separate credentials.
var rssB2Authorization;
// A cached B2 token can be revoked or expire before its nominal lifetime; B2 answers
// 401 expired_auth_token. Without this the isolate serves 401s to podcast clients
// until it is recycled, so reauthorize once and replay the request.
async function withFreshB2Auth(authorize, attempt) {
  const response = await attempt(await authorize(false));
  if (response.status !== 401) return response;
  return attempt(await authorize(true));
}
__name(withFreshB2Auth, "withFreshB2Auth");
function rewritePath(pathname) {
  if (pathname.endsWith("/")) {
    return `${pathname}index.html`;
  }
  return pathname;
}
__name(rewritePath, "rewritePath");
var b2Authorization;
async function authorizePrivateB2(env, force = false) {
  if (!env.B2_APPLICATION_KEY_ID || !env.B2_APPLICATION_KEY) {
    throw new Error("Private B2 credentials are not configured");
  }
  if (!force && b2Authorization && b2Authorization.expiresAt > Date.now()) {
    return b2Authorization;
  }
  const authorization = `Basic ${btoa(`${env.B2_APPLICATION_KEY_ID}:${env.B2_APPLICATION_KEY}`)}`;
  const response = await fetch("https://api.backblazeb2.com/b2api/v4/b2_authorize_account", {
    method: "POST",
    headers: {
      Authorization: authorization,
      "Content-Type": "application/json"
    },
    body: "{}"
  });
  if (!response.ok) {
    throw new Error(`B2 authorization failed (${response.status})`);
  }
  const account = await response.json();
  b2Authorization = {
    authorizationToken: account.authorizationToken,
    downloadUrl: account.apiInfo.storageApi.downloadUrl,
    expiresAt: Date.now() + 12 * 60 * 60 * 1e3
  };
  return b2Authorization;
}
__name(authorizePrivateB2, "authorizePrivateB2");
async function proxyPrivateNewsl(request, env) {
  const url = new URL(request.url);
  const rewritten = rewritePath(url.pathname).replace(/^\/newsl\/?/, "");
  const path = rewritten.split("/").map(encodeURIComponent).join("/");
  const bucket = env.B2_PRIVATE_BUCKET ?? "newsl-private-05802";
  const forwarded = {};
  for (const name of ["if-none-match", "if-modified-since", "range"]) {
    const value = request.headers.get(name);
    if (value) forwarded[name] = value;
  }
  const attempt = (account) => fetch(
    `${account.downloadUrl}/file/${bucket}/${path}${url.search}`,
    {
      method: request.method === "HEAD" ? "HEAD" : "GET",
      headers: { ...forwarded, Authorization: account.authorizationToken }
    }
  );
  const response = await withFreshB2Auth((force) => authorizePrivateB2(env, force), attempt);
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", "private, no-store");
  return new Response(response.body, { status: response.status, headers });
}
__name(proxyPrivateNewsl, "proxyPrivateNewsl");
async function authorizeRssB2(env, force = false) {
  if (!env.B2_RSS_KEY_ID || !env.B2_RSS_KEY || !env.B2_RSS_BUCKET) {
    throw new Error("Private RSS B2 origin is not configured");
  }
  if (!force && rssB2Authorization && rssB2Authorization.expiresAt > Date.now()) return rssB2Authorization;
  const response = await fetch("https://api.backblazeb2.com/b2api/v4/b2_authorize_account", {
    method: "POST",
    headers: { Authorization: `Basic ${btoa(`${env.B2_RSS_KEY_ID}:${env.B2_RSS_KEY}`)}`, "Content-Type": "application/json" },
    body: "{}"
  });
  if (!response.ok) throw new Error(`RSS B2 authorization failed (${response.status})`);
  const account = await response.json();
  rssB2Authorization = {
    authorizationToken: account.authorizationToken,
    downloadUrl: account.apiInfo.storageApi.downloadUrl,
    expiresAt: Date.now() + 12 * 60 * 60 * 1e3
  };
  return rssB2Authorization;
}
async function proxyToB2(request, env) {
  if (request.method !== "GET" && request.method !== "HEAD") return text("Method Not Allowed", { status: 405 });
  const url = new URL(request.url);
  if (url.pathname.endsWith("/README.md")) return text("Not Found", { status: 404 });
  const path = rewritePath(url.pathname).replace(/^\//, "").split("/").map((part) => encodeURIComponent(decodeURIComponent(part))).join("/");
  const forwarded = {};
  for (const name of ["range", "if-range", "if-none-match", "if-modified-since"]) {
    const value = request.headers.get(name);
    if (value) forwarded[name] = value;
  }
  const attempt = (account) => fetch(
    `${account.downloadUrl}/file/${encodeURIComponent(env.B2_RSS_BUCKET)}/${path}`,
    { method: request.method, headers: { ...forwarded, Authorization: account.authorizationToken } }
  );
  const response = await withFreshB2Auth((force) => authorizeRssB2(env, force), attempt);
  const headers = new Headers();
  for (const name of ["content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified", "content-encoding"]) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", response.ok ? "public, max-age=3600" : "no-store");
  // Stored skeptic-tank XML can contain obsolete enclosure hostnames.
  if (url.pathname === "/skeptic-tank/podcast.xml" && response.ok && request.method === "GET") {
    const xml = (await response.text()).replace(/https?:\/\/(?:rss\.stations\.work|rss-b2-proxy\.aaronkuna\.workers\.dev|rss-b2-proxy\.05802\.workers\.dev)/g, PUBLIC_BASE);
    headers.delete("content-length");
    headers.delete("etag");
    headers.delete("content-encoding");
    headers.set("Content-Type", "application/rss+xml; charset=utf-8");
    headers.set("Cache-Control", "public, max-age=60");
    return new Response(xml, { status: response.status, headers });
  }
  return new Response(request.method === "HEAD" ? null : response.body, { status: response.status, headers });
}
__name(proxyToB2, "proxyToB2");

// src/index.ts
function episodeIdFromPath(pathname) {
  const match = pathname.match(/^\/api\/episodes\/(\d+)$/);
  return match ? Number.parseInt(match[1], 10) : null;
}
__name(episodeIdFromPath, "episodeIdFromPath");
function feedIdFromPath(pathname) {
  const match = pathname.match(/^\/api\/feeds\/([^/]+)$/);
  return match ? match[1] : null;
}
__name(feedIdFromPath, "feedIdFromPath");
// Credentialed and operational paths. Cloudflare's cache key contains neither the
// Authorization header nor the Cookie, and a 200 with no Cache-Control is cached
// heuristically for two hours, so a token-gated body must say no-store itself
// before Workers Caching (`[cache] enabled`) is ever turned on for this Worker.
function isPrivatePath(pathname) {
  return pathname === "/healthz" || pathname === "/admin" || pathname === "/newsl" ||
    pathname.startsWith("/api/") || pathname.startsWith("/admin/") || pathname.startsWith("/newsl/");
}
__name(isPrivatePath, "isPrivatePath");
var index_default = {
  async fetch(request, env) {
    const response = await route(request, env);
    const pathname = new URL(request.url).pathname;
    // Any successful API write may change a feed: drop the stored copies; the next GET rebuilds.
    if (pathname.startsWith("/api/") && ["POST", "PUT", "DELETE"].includes(request.method) && response.ok) {
      await env.DB.prepare("DELETE FROM feed_gz").bind().run();
    }
    if (isPrivatePath(pathname)) {
      const headers = new Headers(response.headers);
      headers.set("Cache-Control", "private, no-store");
      return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
    }
    return response;
  }
};
var router_default = {
  async fetch(request, env) {
    const url = new URL(request.url);
    const pathname = url.pathname;
    if (pathname === "/newsl" || pathname.startsWith("/newsl/")) {
      if (!authorizeNewsl(request, env)) {
        return unauthorizedNewsl();
      }
    }
    if (request.method === "OPTIONS" && pathname.startsWith("/api/")) {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "authorization, content-type",
          "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"
        }
      });
    }
    if (pathname === "/podcast.xml") {
      return new Response("Not Found", { status: 404 });
    }
    const feedMatch = pathname.match(/^\/([^/]+)\/podcast\.xml$/);
    if (feedMatch && isFeedId(feedMatch[1])) {
      return generateFeedResponse(request, env, feedMatch[1]);
    }
    if (pathname === "/admin" || pathname.startsWith("/admin/")) {
      return renderAdmin();
    }
    if (pathname.startsWith("/api/")) {
      const authError = authorizeApi(request, env);
      if (authError) {
        return authError;
      }
      if (pathname === "/api/episodes" && request.method === "GET") {
        return listEpisodes(request, env);
      }
      if (pathname === "/api/episodes" && request.method === "POST") {
        return createEpisode(request, env);
      }
      const episodeId = episodeIdFromPath(pathname);
      if (episodeId !== null) {
        if (request.method === "GET") {
          return getEpisode(episodeId, env);
        }
        if (request.method === "PUT") {
          return updateEpisode(episodeId, request, env);
        }
        if (request.method === "DELETE") {
          return deleteEpisode(episodeId, env);
        }
      }
      if (pathname === "/api/feeds" && request.method === "GET") {
        return listFeeds(env);
      }
      const feedId = feedIdFromPath(pathname);
      if (feedId && request.method === "PUT") {
        return updateFeed(feedId, request, env);
      }
      return json({ error: "API route not found" }, { status: 404 });
    }
    if (pathname === "/healthz") {
      return text("ok");
    }
    if (pathname === "/newsl" || pathname.startsWith("/newsl/")) {
      return proxyPrivateNewsl(request, env);
    }
    return proxyToB2(request, env);
  }
};
function route(request, env) {
  return router_default.fetch(request, env);
}
__name(route, "route");
export {
  index_default as default
};
//# sourceMappingURL=index.js.map
