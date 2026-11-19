"""Every Surveillance notes terminal check has an earlier stage that holds its rule (F-144).

| case                                | proves                                                       |
| the ten checks, rule by rule        | for each rule a model controls, the gate that sees that      |
|                                     | answer rejects it with a message naming the rule (lede thin  |
|                                     | and pronoun-first, naming, meta, quote words and paragraph,  |
|                                     | mainPoints count on check 8's own floor)                     |
| a rank whose clocks walk backwards  | the notes sort main points by clock before the render, so    |
|                                     | check 8 passes; the section count is the list the renderer    |
|                                     | wrote, so check 1 passes                                     |
| a repair that still fails the gate  | run() raises before the ranking call is paid for             |
| "the guest" in a mention or a rank  | check 5's rule reaches the mention line and the summary       |
| a firm named against itself         | firm_once writes it once, so check 6's other clause holds     |

`checks()` is unchanged: these hold the same rules earlier, in the gate that sees the model's answer.
"""
import json
import re
from types import SimpleNamespace

import pytest

from episode_enrichment.paragraphs import build_stream

import surveillance_notes as SN

# ---------------------------------------------------------------- the gate's own rules, one payload each

SCRIPT = """Kabra says central banks are buying gold bars at a record pace this autumn.

The bars go to their vaults in London, where the bank keeps them.

Kabra says the buying is a reserve shift, not a trade.
"""

LEDE = "Kabra says central banks are buying gold bars at a record pace."
BULLET = "Kabra says the bars go to their vaults in London every month."


def _stream():
    return build_stream(SCRIPT)


def _part(**over):
    fields = {"lo": 0, "hi": 2, "words": 27, "guests": ("Manish Kabra",), "owners": frozenset({"Kabra"}),
              "firms": {"Manish Kabra": "Societe Generale"}, "title": "", "topic": "", "about": "",
              "first": "p00001", "last": "p00003", "k": 1}
    fields.update(over)
    return SN.Part(**fields)


def answer(**over):
    """One section the gate accepts, with the named field replaced by the case's own slip."""
    section = {"heading": "Gold demand", "lede": LEDE, "startParagraphId": "p00001",
               "endParagraphId": "p00003", "bullets": [{"text": BULLET, "paragraphIds": ["p00001"]}],
               "mentions": []}
    section.update(over)
    return {"sections": [section]}


def _mention(**over):
    mention = {"type": "person", "displayText": "Christine Lagarde", "note": "the ECB's gold buyer",
               "paragraphId": "p00002", "quote": {"words": 6}}
    mention.update(over)
    return mention


RANK_BULLETS = ["0:00", "1:00", "3:00", "4:30", "9:30", "12:00"]
RANK_CLOCKS = ["0:00", "1:00", "3:00", "4:30", "9:30", "12:00"]
SUMMARY = "Kabra says the banks are buying gold at a record pace."


def _rank_gate():
    """A rank gate whose bullets are two sections long: check 8's floor here is two points, not four."""
    bullets = [{"text": f"bullet {n}", "clock": clock, "heading": "Gold demand", "run": 1}
               for n, clock in enumerate(RANK_BULLETS)]
    return SN.rank_gate(bullets, ["0:00", "9:30"])


def _rank(points, summary=SUMMARY):
    return {"summary": summary,
            "mainPoints": [{"text": f"Point {n} on gold.", "bullet": index}
                           for n, index in enumerate(points)]}


CASES = {
    # rule 2: a rule the model controls is held in the gate that sees the model's answer
    "lede-thin": (lambda: SN.section_gate(_stream(), _part()), lambda: answer(lede="Kabra says gold is up."),
                  "its lede is 5 words"),
    "lede-pronoun": (lambda: SN.section_gate(_stream(), _part()),
                     lambda: answer(lede="They say central banks are buying gold bars at a record pace."),
                     "its lede opens with 'They'"),
    "naming": (lambda: SN.section_gate(_stream(), _part()),
               lambda: answer(bullets=[{"text": "Gold is bid at a record pace by central banks.",
                                        "paragraphIds": ["p00001"]}]),
               "first bullet must start with the speaker's surname"),
    "meta": (lambda: SN.section_gate(_stream(), _part()),
             lambda: answer(bullets=[{"text": "Kabra says the roster is short this month, and the bars go to "
                                               "their vaults.",
                                      "paragraphIds": ["p00001"]}]),
             "roster"),
    "quote-words": (lambda: SN.section_gate(_stream(), _part()),
                    lambda: answer(mentions=[_mention(quote={"words": 2})]),
                    "quote.words must be a whole number from 3 to 15"),
    "quote-paragraph": (lambda: SN.section_gate(_stream(), _part()),
                        lambda: answer(mentions=[_mention(paragraphId="p00099")]),
                        "paragraph p00099 is outside the section"),
    "mainPoints-count": (_rank_gate, lambda: _rank([0]), "mainPoints: give 2 to 6"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_terminal_check_has_a_holding_stage(case):
    gate, payload, wanted = CASES[case]
    errors = gate()(payload())
    assert any(wanted in error for error in errors), errors
    if case == "mainPoints-count":
        # check 8's floor is the distinct section clocks: two sections take two points, never four
        assert gate()(_rank([0, 4])) == []


# ---------------------------------------------------------------- rule 3: the code makes its rules true

HEADING = re.compile(r"^\[(p\d{5})\] ", re.M)
K_SECTIONS = re.compile(r"Write (\d+) sections")
SPEAKER = re.compile(r"The speakers? (?:is|are) (.+?)\.\n")
PARAGRAPHS = 6


def _section_runner(points=None):
    """A fake model for both prompt shapes: gate-passing sections, and a ranking it repeats verbatim.

    A two-paragraph section's bullets are written in the reverse of its paragraphs' order, so the bullets
    the ranking is given walk the clocks backwards — exactly the shape check 8 rejects.
    """
    calls = []

    def run(prompt, *, allow_tools=False, timeout_s=None, session_name=None):
        calls.append(session_name)
        if "WHAT THIS PART IS ABOUT" in prompt:
            ids = HEADING.findall(prompt)
            k = int(K_SECTIONS.search(prompt).group(1))
            who = SPEAKER.search(prompt).group(1).split(" of ")[0]
            surname = who.split()[-1]
            bounds = [ids[i * len(ids) // k:(i + 1) * len(ids) // k] for i in range(k)]
            sections = []
            for chunk in bounds:
                cited = list(reversed(chunk)) if len(chunk) > 1 else chunk
                sections.append({"heading": f"Gold and the banks {len(sections) + 1}",
                                 "lede": f"{who} says the panel weighed the yield curve, the dollar and the "
                                         f"labour market in turn.",
                                 "startParagraphId": chunk[0], "endParagraphId": chunk[-1],
                                 "bullets": [{"text": f"{surname} says the banks are buying gold bars again.",
                                              "paragraphIds": [pid]} for pid in cited],
                                 "mentions": []})
            return SimpleNamespace(ok=True, text=json.dumps({"sections": sections}), error=None)
        assert "You rank the notes" in prompt
        indices = [int(n) for n in re.findall(r"^\[(\d+)\] ", prompt, re.M)]
        cited = points if points is not None else list(range(min(4, len(indices))))
        return SimpleNamespace(ok=True, text=json.dumps(_rank(cited)), error=None)

    return run, calls


def _broken_runner():
    calls = []

    def run(prompt, *, allow_tools=False, timeout_s=None, session_name=None):
        calls.append(session_name)
        return SimpleNamespace(ok=True, text=json.dumps({"sections": []}), error=None)

    return run, calls


def _show():
    """Two parts of three paragraphs, each asking for two sections, on a cut whose clocks are 40 s apart."""
    script = "\n\n".join(f"Kabra says paragraph {n + 1} of the show is about central banks and gold."
                         for n in range(PARAGRAPHS))
    stream = build_stream(script)
    starts = [n * 40.0 for n in range(PARAGRAPHS)]
    parts = []
    for lo, hi in ((0, 2), (3, 5)):
        parts.append(SN.Part(lo=lo, hi=hi, words=sum(p.words for p in stream.paragraphs[lo:hi + 1]),
                             guests=("Manish Kabra",), owners=frozenset({"Kabra"}),
                             firms={"Manish Kabra": "Societe Generale"}, title="Gold", topic="", about="",
                             first=stream.paragraphs[lo].id, last=stream.paragraphs[hi].id, k=2))
    return stream, starts, parts


def test_a_ranking_whose_clocks_walk_backwards_still_passes_the_checks(tmp_path):
    stream, starts, parts = _show()
    run, calls = _section_runner(points=[0, 1, 2, 3])
    result = SN.run(stream=stream, parts=parts, starts=starts, title="T", work=tmp_path / "w",
                    runner_factory=lambda role: SimpleNamespace(run=run))
    assert calls.count("rank") == 1 and calls.count("rank-repair") == 1   # the gate answered, the repair repeated it
    assert "errorsAfterRepair" in result.report["rank"]
    # rule 3: the renderer's list is the count, and the main points are sorted by clock before the render
    assert len(SN.parse(result.notes)[0]) == result.report["counts"]["topics"] == sum(part.k for part in parts)
    assert [b["clock"] for b in result.main_points] == sorted((b["clock"] for b in result.main_points),
                                                              key=SN.seconds_of)
    checks = {check.name: check for check in SN.checks(
        notes=result.notes, report=result.report, stream=stream, starts=starts, parts=parts)}
    assert checks["sections"].ok and checks["mainPoints"].ok, \
        {name: check.reason for name, check in checks.items() if not check.ok}


def test_a_block_whose_repair_still_fails_the_section_gate_fails_early(tmp_path):
    stream, starts, parts = _show()
    run, calls = _broken_runner()
    with pytest.raises(SN.SectionsError) as error:
        SN.run(stream=stream, parts=parts[:1], starts=starts, title="T", work=tmp_path / "w",
               runner_factory=lambda role: SimpleNamespace(run=run))
    said = str(error.value)
    assert error.value.stage == "sections"
    assert parts[0].first in said and "still fails the section gate" in said and "0 sections" in said
    assert calls == ["i01", "i01-repair"]       # asked once, repaired once, and the ranking call never paid for


# ---------------------------------------------------------------- the rules the gates gained

def test_the_anonymous_rule_reaches_the_mention_line_and_the_rank():
    """Check 5 reads every line of the notes: the mention line and the rank's summary are model prose too."""
    gate = SN.section_gate(_stream(), _part())
    errors = gate(answer(lede="Kabra says central banks are buying gold bars at a record pace.",
                         mentions=[_mention(note="the guest's own view of the buying")]))
    assert any("it says 'the guest'" in error for error in errors), errors
    rank = _rank_gate()
    assert any("it says 'the speaker'" in error
               for error in rank(_rank([0, 4], summary="The speaker says the banks are buying gold.")))
    assert rank(_rank([0, 4])) == []


def test_a_firm_named_against_itself_is_written_once():
    """Check 6 counts "Evercore of Evercore": the same rewrite that holds the name+firm rule holds this."""
    notes = "### 0:00 · The AI trade\n\nJulian Emmanuel of Evercore says A.\n\n- Evercore of Evercore is the buyer.\n"
    held = SN.firm_once(notes, {"Julian Emmanuel": "Evercore"})
    assert "Evercore of Evercore" not in held and "Julian Emmanuel of Evercore says A." in held


# ---------------------------------------------------------------- a stray citation is dropped, not fatal

def test_a_citation_outside_its_section_is_dropped_after_the_repair():
    """Radio Nov 18 (af13dc03): a bullet citing a paragraph past its section's end killed the whole job."""
    stream, part = _stream(), _part()
    payload = answer(endParagraphId="p00002",
                     bullets=[{"text": BULLET, "paragraphIds": ["p00001", "p00003"]},
                              {"text": "Kabra says the vaults are in London.", "paragraphIds": ["p00003"]}],
                     mentions=[_mention(paragraphId="p00003"), _mention(paragraphId="p00002")])
    payload["sections"].append({"heading": "Reserves", "lede": LEDE, "startParagraphId": "p00003",
                                "endParagraphId": "p00003",
                                "bullets": [{"text": BULLET, "paragraphIds": ["p00003"]}], "mentions": []})
    gate = SN.section_gate(stream, _part(k=2))
    assert any("p00003 is outside the section" in error for error in gate(payload))
    assert SN.refs_held(stream, part, payload) == 3
    first = payload["sections"][0]
    assert first["bullets"] == [{"text": BULLET, "paragraphIds": ["p00001"]}]
    assert [m["paragraphId"] for m in first["mentions"]] == ["p00002"]
    assert gate(payload) == []
    assert payload["sections"][1]["bullets"][0]["paragraphIds"] == ["p00003"]   # in its own span: untouched


def test_a_section_whose_span_is_wrong_is_left_for_the_gate():
    payload = answer(endParagraphId="p00099", bullets=[{"text": BULLET, "paragraphIds": ["p00098"]}])
    assert SN.refs_held(_stream(), _part(), payload) == 0
    assert payload["sections"][0]["bullets"][0]["paragraphIds"] == ["p00098"]


def test_a_run_whose_repair_still_cites_outside_a_section_finishes(tmp_path):
    stream, starts, parts = _show()
    good, calls = _section_runner()

    def run(prompt, **kw):
        got = good(prompt, **kw)
        if "WHAT THIS PART IS ABOUT" not in prompt:
            return got
        data = json.loads(got.text)
        last = data["sections"][-1]["endParagraphId"]
        data["sections"][0]["bullets"].append({"text": "Kabra says gold bars are scarce.", "paragraphIds": [last]})
        return SimpleNamespace(ok=True, text=json.dumps(data), error=None)

    result = SN.run(stream=stream, parts=parts, starts=starts, title="T", work=tmp_path / "w",
                    runner_factory=lambda role: SimpleNamespace(run=run))
    assert calls.count("i01-repair") == 1 and calls.count("rank") == 1
    assert [r.get("refsDropped") for r in result.report["runs"]] == [1, 1]
    assert len(SN.parse(result.notes)[0]) == sum(part.k for part in parts)
