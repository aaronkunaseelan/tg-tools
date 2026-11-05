"""The episode page (episode_enrichment/page.py): notes markdown + script -> one HTML document.

| case                     | proves                                                                    |
| render.py's whole subset | single and sectioned notes convert with no raw markdown left; lists nest   |
| deterministic            | the same input gives the same bytes (no clock)                            |
| references               | every pNNNNN in the notes links to a script paragraph id that exists     |
| times                    | a paragraph shows its start only when the timings belong to the script   |
| escaping                 | HTML in model text is escaped; a non-http link is kept as text           |
"""
import json
import re

from episode_enrichment import render, timing
from episode_enrichment.page import notes_html, render_page
from episode_enrichment.paragraphs import build_stream

SCRIPT = "\n\n".join(f"Sentence number {n} about markets." for n in range(1, 7))
ANALYSIS = {"summary": "Rates <b>up</b> & **bonds** [x](javascript:alert(1)).",
            "topics": [{"heading": "Rates", "startParagraphId": "p00001", "endParagraphId": "p00003"},
                       {"heading": "Oil", "startParagraphId": "p00004", "endParagraphId": "p00006"}]}
MENTIONS = {"topics": [{"heading": "Rates", "mentions": [
    {"type": "person", "displayText": "Jay <Powell>", "referencedAs": "the chair", "paragraphId": "p00002",
     "evidenceQuote": "number 2"}]}]}


def notes(sections=False):
    stream = build_stream(SCRIPT)
    return render.render_notes_markdown(
        stream=stream, analysis=ANALYSIS, mentions=MENTIONS, title="Show 11/3/2026", published="2026-11-03T12:00:00Z",
        duration_seconds=10800, mp3_url="https://x/a.mp3", script_url="https://x/a.script.md",
        sections=[("Hour 1 · 07:00–08:00 EST", ANALYSIS, MENTIONS), ("Hour 2 · 08:00–09:00 EST", ANALYSIS, MENTIONS)]
        if sections else None)


def text_of(page):
    return re.sub(r"<[^>]+>", "", page.split("</style>", 1)[1])


def test_the_whole_subset_converts():
    for sectioned in (False, True):
        page = render_page(notes_markdown=notes(sectioned), script_text=SCRIPT)
        body = text_of(page).replace("[x](javascript:alert(1))", "")   # the refused link, kept as text
        assert not [m for m in ("**", "](", "\n# ", "\n## ", "\n### ", "\n- ") if m in body], sectioned
        assert page.count("<ul>") == page.count("</ul>") and page.count("<li>") == page.count("</li>")
        assert "<h1>Show 11/3/2026</h1>" in page and '<a href="https://x/a.mp3">MP3</a>' in page
    sectioned = render_page(notes_markdown=notes(True), script_text=SCRIPT)
    assert "<h2>Hour 1 · 07:00–08:00 EST</h2>" in sectioned and "<h3>Topics</h3>" in sectioned
    assert re.search(r"<li><strong>person: Jay &lt;Powell&gt;</strong> — the chair<ul><li>Evidence \(<a "
                     r'href="#p00002">p00002</a>\): “number 2”</li></ul></li>', sectioned.replace("\n", ""))


def test_notes_and_script_are_one_article():
    """A reader's readability stage keeps one article: the script must be inside it, after the notes."""
    page = render_page(notes_markdown=notes(True), script_text=SCRIPT)
    assert page.count("<article") == 1 and page.count("</article>") == 1
    assert page.index("<article") < page.index('<section class="notes"') < page.index('<section class="script"') < page.index("</article>")


def test_deterministic_bytes():
    assert render_page(notes_markdown=notes(True), script_text=SCRIPT) == render_page(
        notes_markdown=notes(True), script_text=SCRIPT)


def test_every_reference_links_to_a_paragraph():
    page = render_page(notes_markdown=notes(True), script_text=SCRIPT)
    ids = set(re.findall(r'id="(p\d{5})"', page))
    assert ids == {f"p0000{n}" for n in range(1, 7)}
    assert set(re.findall(r'href="#(p\d{5})"', page)) <= ids


def test_times_only_with_the_scripts_own_timings():
    units = [{"start": 60.0 * n, "end": 60.0 * n + 5, "text": f"Sentence number {n + 1} about markets."} for n in range(6)]
    timed, _ = timing.build(SCRIPT, {"basis": "segments", "units": units, "segments": units})
    page = render_page(notes_markdown=notes(), script_text=SCRIPT, timed=timed)
    assert '<p id="p00002"><a class="t" href="#p00002">0:01:00</a>Sentence number 2' in page
    other = dict(json.loads(timed), paragraphs=json.loads(timed)["paragraphs"][:3])   # not this script's
    assert '<a class="t" href="#p00002">p00002</a>' in render_page(notes_markdown=notes(), script_text=SCRIPT, timed=other)


def test_model_text_is_escaped_and_only_http_links_are_kept():
    html = notes_html(notes())
    assert "Rates &lt;b&gt;up&lt;/b&gt; &amp; <strong>bonds</strong>" in html
    assert "javascript:" not in html.split("[x](")[0] and 'href="javascript' not in html
    assert "[x](javascript:alert(1))" in html   # kept as text


def test_the_page_needs_nothing_the_sandbox_refuses():
    """rss-edge serves page.html with CSP sandbox: no scripts, opaque origin. Links and inline styles work."""
    page = render_page(notes_markdown=notes(True), script_text=SCRIPT)
    assert "<script" not in page.lower() and not re.search(r"<[^>]*\son\w+\s*=", page, re.I)   # no event handlers
    assert not re.search(r"<(img|link|iframe|object|embed|video|audio|source)\b", page, re.I)
    assert not re.search(r"@import|url\(", page) and "src=" not in page
