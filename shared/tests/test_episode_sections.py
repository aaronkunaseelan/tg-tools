"""An ordinary episode's notes on the shared renderer (X-104 upstream).

The episodes pipeline makes two model calls per episode — the topics pass and the mentions pass — and this
packet adds nothing to that budget: the lede and the ranked main points are fields of the topics call, and
everything else (names, firms, clocks, quotes) is code. The table below is what these tests hold:

| what                                     | where                                        |
| the same two calls, in the same order    | test_an_ordinary_episode_makes_exactly_two_calls, test_the_pinned_topics_prompt_is_unchanged |
| lede required, main points in the schema | test_validate_topics_sections                 |
| gates report, never repair               | test_the_form_gates_never_cost_a_call         |
| code writes name, firm, clock, quote     | test_names_firms_clocks_and_quotes_are_code_written, test_quote_from |
| nobody named in metadata: no sentence    | test_the_speaker_fact_is_one_sentence_or_absent |
"""
import json
import re
from types import SimpleNamespace

import pytest

from episode_enrichment import enrich_script, timing
from episode_enrichment import prompts
from episode_enrichment.paragraphs import build_stream
from episode_enrichment.render import quote_from, render_episode_sections
from episode_enrichment.speakers import person_name, speaker_entry, speaker_fact
from episode_enrichment.validate import notes_gates, validate_topics

SCRIPT = "\n\n".join([
    "Jane Doe says heating a polypropylene bottle released millions of particles per litre.",
    "Doe says the exposure estimate stays uncertain, since nobody has counted what people breathe.",
    "Doe adds that the method itself is new: the team counted particles down to a micrometre.",
    "The authors call for a standard, so the next study can be compared with this one.",
])
STREAM = build_stream(SCRIPT)
IDS = STREAM.ids


def timed_transcript():
    words = [{"word": word, "start": float(index), "end": index + 0.5}
             for index, word in enumerate(SCRIPT.split())]
    text, _ = timing.build(SCRIPT, timing.from_self_hosted({"words": words, "segments": None}))
    return text


class StoredSource:
    """A published script read back from the store: not speech, so its reader passes no timings of its own."""
    kind, content_origin, topic_origin, summary_origin = "audio", "stt_transcript", "luna_topics", "luna_summary"
    has_preknown_topics = False


def script(timed=True):
    from episode_enrichment.pipeline import PreparedScript
    return PreparedScript(text=SCRIPT, path=None, sha256=__import__("hashlib").sha256(SCRIPT.encode()).hexdigest(),
                          source=StoredSource(), timed_transcript=timed_transcript() if timed else None)


def topics_answer(*, lede=True, main_points=True, quotes=None):
    """The topics pass's answer: two segments, the ledes the shape asks for, two ranked points."""
    topics = [{"heading": "Particles per litre",
               "startParagraphId": IDS[0], "endParagraphId": IDS[1]},
              {"heading": "What the study does not say", "startParagraphId": IDS[2], "endParagraphId": IDS[3]}]
    body = {"summary": "A study counted particles shed by a plastic bottle."}
    if lede:
        for topic, text in zip(topics, [
                "Doe says the team counted millions of particles released by a heated bottle.",
                "Doe says nobody has counted what a person actually breathes in."]):
            topic["lede"] = text
    if main_points:
        body["mainPoints"] = [{"text": "Doe says a litre held millions of particles.",
                               "paragraphId": IDS[0]},
                              {"text": "Doe says the exposure estimate is open.", "paragraphId": IDS[1]}]
    body["topics"] = topics
    return body


MENTIONS = {"topics": [{"heading": "Particles per litre",
                        "mentions": [{"type": "organization", "displayText": "WHO",
                                      "referencedAs": "Doe cites the drinking-water guidance.",
                                      "paragraphId": IDS[0],
                                      "evidenceQuote": "released millions of particles per litre"}]},
                       {"heading": "What the study does not say",
                        "mentions": [{"type": "product_or_service", "displayText": "Polypropylene",
                                      "referencedAs": "The bottle under test was made of it.",
                                      "paragraphId": IDS[2],
                                      "evidenceQuote": "the team counted particles down to a micrometre"}]}]}


class Counting:
    """The two fake answers, and the record of what was asked: one list entry per model call."""

    def __init__(self, *, topics=None, mentions=None):
        self.calls, self.topics, self.mentions = [], topics or topics_answer(), mentions or MENTIONS

    def factory(self, role):
        runner = self

        class Runner:
            def run(self, prompt, allow_tools=False, timeout_s=None, session_name=""):
                runner.calls.append({"role": role, "session": session_name, "prompt": prompt})
                answer = runner.topics if "startParagraphId" in prompt else runner.mentions
                return SimpleNamespace(ok=True, text=json.dumps(answer), error=None)
        return Runner()


def enrich(tmp_path, *, counted=None, speaker=None, timed=True, notes_shape="sections"):
    counted = counted or Counting()
    return counted, enrich_script(
        script=script(timed), work_dir=tmp_path, title="Bottles", published="2026-11-05",
        duration_seconds=1800, notes_shape=notes_shape, speaker=speaker,
        runner_factory=counted.factory)


def test_an_ordinary_episode_makes_exactly_two_calls(tmp_path):
    counted, result = enrich(tmp_path)
    assert [call["session"] for call in counted.calls] == ["transcript-topics", "transcript-mentions"]
    assert len(counted.calls) == 2                       # the budget the whole packet lives inside
    assert "Doe says the team counted millions of particles" in result.notes_markdown
    assert "## Main points" in result.notes_markdown
    assert result.reports["notes"]["ok"] and result.reports["notes"]["stats"]["sections"] == 2


def test_the_pinned_topics_prompt_is_unchanged(tmp_path):
    """The reader pipelines' prompt is the template with the three sections fields empty: same bytes."""
    old = prompts.TOPICS_PROMPT.format(
        untrusted=prompts.UNTRUSTED, summary_target=prompts.SUMMARY_TARGET_CHARS,
        first=STREAM.first_id(), last=STREAM.last_id(), body=prompts.render_body(STREAM),
        sections_task="", main_points_field="", lede_field="", speaker_block="", source_block="")
    assert prompts.topics_prompt(STREAM) == old
    assert "lede" not in old and "mainPoints" not in old and "WHO SPEAKS" not in old
    assert "SOURCE DESCRIPTION" not in old
    sections = prompts.topics_prompt(STREAM)
    assert "mainPoints" not in sections and "WHO SPEAKS" not in sections


@pytest.mark.parametrize("make, error", [
    (lambda body: body.pop("mainPoints"), "no mainPoints"),
    (lambda body: body["mainPoints"][0].pop("text"), "mainPoints[0] is empty"),
    (lambda body: body["mainPoints"][0].update(paragraphId="p09999"), "not a transcript paragraph"),
    (lambda body: body.update(mainPoints=body["mainPoints"][:1]), "give 2 to 6"),
    (lambda body: body["topics"][1].pop("lede"), "has no lede"),
    (lambda body: body["topics"][0].update(lede="  "), "has no lede"),
])
def test_validate_topics_sections(make, error):
    body = topics_answer()
    make(body)
    report = validate_topics(STREAM, body, sections=True)
    assert error in " ".join(report.errors)
    # the same answer without the sections shape is not asked for ledes or points
    assert not any(error in note for note in validate_topics(STREAM, body).errors)


def test_the_sections_payload_passes_and_counts_its_fields():
    report = validate_topics(STREAM, topics_answer(), sections=True)
    assert report.ok and report.stats["ledes"] == 2 and report.stats["mainPoints"] == 2


def test_the_form_gates_never_cost_a_call(tmp_path):
    """Doctored answers: a pronoun-first lede, a two-word quote, a note about its own instructions."""
    doctored = topics_answer()
    doctored["topics"][0]["lede"] = "Their verdict is that a litre held millions of particles."
    mentions = json.loads(json.dumps(MENTIONS))
    mentions["topics"][0]["mentions"][0]["referencedAs"] = (
        "See the paragraph id above for the roster; they list its members.")
    mentions["topics"][0]["mentions"][0]["evidenceQuote"] = "millions of"
    counted = Counting(topics=doctored, mentions=mentions)
    _, result = enrich(tmp_path, counted=counted)
    assert len(counted.calls) == 2                       # a failure is the report's, never a third call
    stats = result.reports["notes"]["stats"]
    assert stats["ledesMissing"] == 1 and stats["referenceErrors"] >= 1
    assert stats["metaWords"] >= 1 and stats["quotesOutOfRange"] == 1
    assert any("opens with 'Their'" in note for note in result.reports["notes"]["notes"])
    manifest = json.loads(result.manifest)
    assert manifest["gates"]["notes"]["stats"] == stats


def test_names_firms_clocks_and_quotes_are_code_written(tmp_path):
    """One known speaker: the full name and firm once, the surname after; every clock; the transcript's words."""
    counted, result = enrich(tmp_path, speaker={"name": "Jane Doe", "firm": "Bottles Lab"})
    assert len(counted.calls) == 2
    assert "The speaker is Jane Doe of Bottles Lab." in counted.calls[0]["prompt"]
    notes = result.notes_markdown
    body = notes.split("## Topics\n\n", 1)[1]
    assert notes.count("Jane Doe") == 1                  # main points first: the first appearance wins there
    assert "- 0:00 · Jane Doe of Bottles Lab says a litre held millions of particles." in notes
    assert "- 0:13 · Doe says the exposure estimate is open." in notes
    assert "### 0:00 · Particles per litre" in body and "### 0:27 · What the study does not say" in body
    assert "Doe says nobody has counted what a person actually breathes in." in body
    # the quote is cut from the transcript by code, so it carries the transcript's own bytes
    assert 'Evidence (p00001 · 0:00): “released millions of particles per litre.”' in body
    assert "Jane Doe of Bottles Lab" not in body          # introduced once, in the main points above


def test_no_clock_without_timings(tmp_path):
    _, result = enrich(tmp_path, timed=False)
    assert "### Particles per litre" in result.notes_markdown and "· 0:" not in result.notes_markdown
    assert "- Doe says a litre held millions of particles." in result.notes_markdown


def test_quote_from():
    texts = {"p00001": "Doe says “millions” of particles, though the count is disputed."}
    assert quote_from(texts, "p00001", {"quote": {"words": 4}}) == "Doe says “millions” of"
    assert quote_from(texts, "p00001", {"evidenceQuote": "of particles, though the count"}) == \
        "of particles, though the count"
    assert quote_from(texts, "p00001", {"evidenceQuote": "not in the paragraph"}) == ""
    assert quote_from(texts, "p00002", {"quote": {"words": 4}}) == ""
    assert quote_from(texts, "p00001", {"quote": {"words": 2}}) == ""


def test_render_episode_sections_golden():
    speakers = [{"first": IDS[0], "last": IDS[-1], "names": ["Jane Doe"], "firms": {"Jane Doe": "Bottles Lab"}}]
    clocks = {IDS[0]: "0:00", IDS[1]: "0:13", IDS[2]: "0:27"}
    text = render_episode_sections(
        title="Bottles", summary="A study counted particles.", speakers=speakers,
        main_points=[{"text": "Doe says a litre held millions of particles.", "paragraphId": IDS[0]}],
        topics=topics_answer()["topics"], references=MENTIONS["topics"],
        at=lambda paragraph_id: clocks.get(paragraph_id, ""),
        texts={paragraph.id: paragraph.text for paragraph in STREAM.paragraphs},
        published="2026-11-05", duration_seconds=1800, mp3_url="https://x/a.mp3")
    assert text == GOLDEN


GOLDEN = """# Bottles

- Original published: 2026-11-05
- Duration: 00:30:00
- Links: [MP3](https://x/a.mp3)
- Analysis: Machine-generated from the transcript

## Summary

A study counted particles.

## Main points

- 0:00 · Jane Doe of Bottles Lab says a litre held millions of particles.

## Topics

### 0:00 · Particles per litre

Doe says the team counted millions of particles released by a heated bottle.

- **organization: WHO** — Doe cites the drinking-water guidance.
  - Evidence (p00001 · 0:00): “released millions of particles per litre.”

### 0:27 · What the study does not say

Doe says nobody has counted what a person actually breathes in.

- **product: Polypropylene** — The bottle under test was made of it.
  - Evidence (p00003 · 0:27): “the team counted particles down to a micrometre.”
"""


def test_the_speaker_fact_is_one_sentence_or_absent():
    assert speaker_fact(["Jane Doe"], {"Jane Doe": "Bottles Lab"}) == "The speaker is Jane Doe of Bottles Lab."
    assert speaker_fact(["Jane Doe", "Ana Ben"], {"Ana Ben": "WHO"}) == \
        "The speakers are Jane Doe and Ana Ben of WHO."
    assert speaker_fact([]) == "" and speaker_fact([], empty="Say the guest.") == "Say the guest."
    assert speaker_entry("Jane Doe") == (["Jane Doe"], {})
    assert speaker_entry({"name": "Jane Doe", "firm": "Bottles Lab"}) == (["Jane Doe"], {"Jane Doe": "Bottles Lab"})


@pytest.mark.parametrize("text, name", [
    ("Jane Doe", "Jane Doe"), ("Travis K", "Travis K"), ("Mary Jane Watson", "Mary Jane Watson"),
    ("Jean-Luc Picard", "Jean-Luc Picard"),
    ("TBPN", None), ("The Daily", None), ("Bloomberg Surveillance TV", None),
    ("Jane", None), ("Jane Doe 2", None), ("Hard Fork Show", None), ("", None),
    # a title, not a name: the colon, the comma and the ampersand are what episode titles carry
    ("Palmer Weiss: America", None), ("Sam Altman: OpenAI", None), ("Doe, Jane", None),
])
def test_person_name(text, name):
    assert person_name(text) == name


def test_the_shape_is_the_document_and_never_the_calls(tmp_path):
    """An unknown shape, or a segmenter with the sections shape, is refused before any call."""
    counted = Counting()
    with pytest.raises(ValueError, match="unknown notes shape"):
        enrich_script(script=script(), work_dir=tmp_path, title="T", notes_shape="blocks",
                      runner_factory=counted.factory)
    assert counted.calls == []


def test_the_notes_gates_count_without_errors():
    report = notes_gates({"topics": [{"heading": "H", "lede": "They said a thing about the market and its "
                                                                "participants today."}]},
                         {"topics": [{"heading": "H", "mentions": []}]}, speakers=["Jane Doe"])
    assert report.ok and report.stats["ledesMissing"] == 1 and report.stats["topicsWithoutName"] == 1


def test_the_main_points_cost_the_topics_call_almost_nothing():
    """Item 5's measurement, as a test: the sections instructions add a bounded number of words, so the
    topics call's prompt and answer stay inside the role's limits (measured on a stored 10,391-word
    episode: +109 words in the prompt, a 991-word answer with five points)."""
    plain, sections = prompts.topics_prompt(STREAM), prompts.topics_prompt(STREAM, sections=True)
    added = len(sections.split()) - len(plain.split())
    assert 0 < added <= 200                      # the lede field, the five-point rule, their examples
    assert len(prompts.SECTIONS_TASK.split()) <= 200
    assert prompts.MAIN_POINTS == 5
    # the only other growth is the answer's own fields: one lede and five points per topic
    assert "lede" in sections and "mainPoints" in sections and "mainPoints" not in plain
