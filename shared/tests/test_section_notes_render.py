"""The section-notes renderer (episode_enrichment.render.render_section_notes): code owns the rest.

| case                                  | result                                                        |
| first appearance                      | "Full Name of Firm" once, then the surname only, per guest     |
| a panel                               | each of its names is introduced once, on its own first mention |
| a lede                                | a plain paragraph between heading and bullets, takes the first |
|                                       | mention; an absent or empty lede leaves the old layout         |
| a topic without bullets               | one line: "*heading*", the lede below it when there is one     |
| a name that does not lead             | left exactly as the model wrote it                             |
| quotes                                | the first N words of a paragraph, 3..15, never a search        |
| clocks and the input                  | the caller's clock only, "·" between clock and heading; the    |
|                                       | topics are not modified (a second render gives the same bytes) |
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from episode_enrichment.render import quote_of, render_section_notes, surname   # noqa: E402

GOLDEN = """# Bloomberg Surveillance TV 11/5/2026 — interviews

Two interviews on yields and gridlock, then a panel on data centre costs.

## Main points

- 0:12 · Manish Kabra of Societe Generale says falling oil has capped bond yields.
- 9:22 · George Pollack of Signum Global expects gridlock and a blue Texas.

## Topics

### 0:12 · Bond yields and oil

- Manish Kabra of Societe Generale says bond yields have made a cyclical peak.
- Kabra adds that oil is the single driver.

Worth noting: Nvidia (Kabra dates the pullback to its circular financing) · OpenAI (Kabra cites its commitments to pay for data centre costs)

### 9:22 · Gridlock and Texas

- George Pollack of Signum Global expects two years of gridlock.
- Pollack says Texas could turn blue.

### 17:17 · Panel on data centre costs

Momei Qu of PSP Growth says only one of roughly 40 data-center ballots passed, and the industry must answer the voters' costs.

- The panel is split on data centre costs.
- Qu says AI commitments will raise costs.

Worth noting: Anthropic (Planned IPO before Thanksgiving)
"""


def fixture():
    """Two guests and a panel: Kabra, then Pollack, then a panel topic the model leaves unattributed."""
    speakers = [
        {"first": "p00001", "last": "p00010", "names": ["Manish Kabra"],
         "firms": {"Manish Kabra": "Societe Generale"}},
        {"first": "p00011", "last": "p00020", "names": ["George Pollack"],
         "firms": {"George Pollack": "Signum Global"}},
        {"first": "p00021", "last": "p00030", "names": ["Momei Qu", "Innes McFee"],
         "firms": {"Momei Qu": "PSP Growth", "Innes McFee": "Oxford Economics"}},
    ]
    topics = [
        {"heading": "Bond yields and oil", "startParagraphId": "p00003",
         "bullets": [
             {"text": "Kabra says bond yields have made a cyclical peak.", "paragraphIds": ["p00003"]},
             {"text": "Kabra adds that oil is the single driver.", "paragraphIds": ["p00005"]},
         ],
         "mentions": [
             {"displayText": "Nvidia", "note": "Kabra dates the pullback to its circular financing",
              "paragraphId": "p00006", "quote": {"words": 6}},
             {"displayText": "OpenAI", "note": "Kabra cites its commitments to pay for data centre costs",
              "paragraphId": "p00007", "quote": {"words": 6}},
         ]},
        {"heading": "Gridlock and Texas", "startParagraphId": "p00013",
         "bullets": [
             {"text": "Pollack expects two years of gridlock.", "paragraphIds": ["p00013"]},
             {"text": "Pollack says Texas could turn blue.", "paragraphIds": ["p00016"]},
         ],
         "mentions": []},
        {"heading": "Panel on data centre costs", "startParagraphId": "p00025",
         "lede": "Qu says only one of roughly 40 data-center ballots passed, and the industry must "
                 "answer the voters' costs.",
         "bullets": [
             {"text": "The panel is split on data centre costs.", "paragraphIds": ["p00025"]},
             {"text": "Qu says AI commitments will raise costs.", "paragraphIds": ["p00026"]},
         ],
         "mentions": [
             {"displayText": "Anthropic", "note": "Planned IPO before Thanksgiving",
              "paragraphId": "p00027", "quote": {"words": 6}},
         ]},
    ]
    clocks = {"p00003": "0:12", "p00005": "0:40", "p00013": "9:22", "p00016": "10:05",
              "p00025": "17:17", "p00026": "17:44"}
    main_points = [
        {"text": "Manish Kabra of Societe Generale says falling oil has capped bond yields.", "clock": "0:12"},
        {"text": "George Pollack of Signum Global expects gridlock and a blue Texas.", "clock": "9:22"},
    ]
    return speakers, topics, clocks, main_points


def render(topics, speakers, clocks=None, main_points=None, summary="A show.", title="A show"):
    clocks = clocks or {}
    return render_section_notes(title=title, summary=summary, main_points=main_points or [], topics=topics,
                                speakers=speakers, at=lambda pid: clocks.get(str(pid), ""))


def test_the_whole_document_is_the_golden_one():
    speakers, topics, clocks, main_points = fixture()
    got = render(topics, speakers, clocks, main_points,
                 summary="Two interviews on yields and gridlock, then a panel on data centre costs.",
                 title="Bloomberg Surveillance TV 11/5/2026 — interviews")
    assert got == GOLDEN
    assert got.endswith("\n") and not got.endswith("\n\n")
    assert "p000" not in got, "a paragraph id was printed"


def test_each_guest_is_named_and_firmed_once_then_kept_by_surname():
    speakers, topics, clocks, _points = fixture()
    body = render(topics, speakers, clocks).split("## Topics\n\n", 1)[1]
    assert body.count("Manish Kabra") == 1 and body.count("George Pollack") == 1
    assert "- Manish Kabra of Societe Generale says bond yields have made a cyclical peak." in body
    assert "- Kabra adds that oil is the single driver." in body
    assert "- George Pollack of Signum Global expects two years of gridlock." in body
    assert "- Pollack says Texas could turn blue." in body
    assert surname("Manish Kabra") == "Kabra" and surname(None) == ""


def test_a_panel_introduces_each_name_on_its_own_first_appearance():
    panel = [{"first": "p00001", "last": "p00009", "names": ["Momei Qu", "Innes McFee"],
              "firms": {"Momei Qu": "PSP Growth", "Innes McFee": "Oxford Economics"}}]
    topics = [
        {"heading": "Costs", "startParagraphId": "p00001",
         "bullets": [{"text": "Qu says AI commitments will raise costs.", "paragraphIds": ["p00001"]},
                     {"text": "Qu adds that Anthropic's IPO is a test.", "paragraphIds": ["p00002"]}]},
        {"heading": "Supply shocks", "startParagraphId": "p00005",
         "bullets": [{"text": "McFee says wages are the key.", "paragraphIds": ["p00005"]}]},
        {"heading": "Wrap-up", "startParagraphId": "p00007", "bullets": []},
        {"heading": "Adoption", "startParagraphId": "p00008",
         "bullets": [{"text": "Qu says slower adoption is good.", "paragraphIds": ["p00008"]}]},
    ]
    out = render(topics, panel, main_points=[{"text": "A point.", "clock": ""}])
    assert "- Momei Qu of PSP Growth says AI commitments will raise costs." in out
    assert "- Qu adds that Anthropic's IPO is a test." in out
    assert "- Innes McFee of Oxford Economics says wages are the key." in out
    assert "- Qu says slower adoption is good." in out
    assert out.count("Momei Qu") == 1 and out.count("Innes McFee") == 1
    assert "\n- A point.\n" in out, "an empty clock was printed"
    assert "\n*Wrap-up*\n" in out, "a bulletless topic did not render as one line"


def test_a_topic_the_model_leaves_unattributed_is_kept_as_written():
    speakers, topics, clocks, _points = fixture()
    body = render(topics, speakers, clocks).split("## Topics\n\n", 1)[1]
    assert "- The panel is split on data centre costs." in body
    assert "Innes McFee" not in body, "a panel name was invented"


def test_a_lede_is_a_plain_paragraph_and_first_use_wins_over_the_bullets():
    speakers = [
        {"first": "p00001", "last": "p00009", "names": ["Momei Qu", "Innes McFee"],
         "firms": {"Momei Qu": "PSP Growth", "Innes McFee": "Oxford Economics"}},
        {"first": "p00010", "last": "p00019", "names": ["Bilal Hafeez"], "firms": {"Bilal Hafeez": "Nomura"}},
    ]
    topics = [
        {"heading": "Costs", "startParagraphId": "p00001", "lede": "Qu says AI commitments will raise costs.",
         "bullets": [{"text": "Qu adds that Anthropic's IPO is a test.", "paragraphIds": ["p00002"]}]},
        {"heading": "Wheat", "startParagraphId": "p00004", "lede": "",
         "bullets": [{"text": "McFee says wages are the key.", "paragraphIds": ["p00004"]}]},
        {"heading": "Adoption", "startParagraphId": "p00006", "lede": "Qu says slower adoption is good.",
         "bullets": []},
        {"heading": "Wages", "startParagraphId": "p00012", "lede": "The panel is split on the wage data.",
         "bullets": [{"text": "Hafeez says wages lag hiring.", "paragraphIds": ["p00012"]}]},
    ]
    out = render(topics, speakers)
    assert ("### Costs\n\nMomei Qu of PSP Growth says AI commitments will raise costs.\n\n"
            "- Qu adds that Anthropic's IPO is a test.") in out
    assert out.count("Momei Qu") == 1, "the lede and the bullets introduced the guest twice"
    assert "### Wheat\n\n- Innes McFee of Oxford Economics says wages are the key." in out
    assert "\n\n\n" not in out, "a blank paragraph crept in"
    assert "*Adoption*\n\nQu says slower adoption is good." in out
    assert "### Wages\n\nThe panel is split on the wage data.\n\n- Bilal Hafeez of Nomura says wages lag hiring." in out


def test_a_quote_is_the_first_words_of_the_paragraph_and_nothing_else():
    texts = {"p00003": "Kabra says bond yields have made a cyclical peak now"}
    assert quote_of(texts, "p00003", 6) == "Kabra says bond yields have made"
    assert quote_of(texts, "p00003", 3) == "Kabra says bond"
    assert quote_of(texts, "p00003", 2) == "" and quote_of(texts, "p00003", 16) == ""
    assert quote_of(texts, "p00003", True) == "" and quote_of(texts, "p99999", 6) == ""


def test_two_guests_with_one_surname_keep_the_surname_alone():
    panel = [{"first": "p00001", "last": "p00009", "names": ["Manish Kabra", "Neel Kabra"],
              "firms": {"Manish Kabra": "Societe Generale", "Neel Kabra": "Nomura"}}]
    topics = [{"heading": "Yields", "startParagraphId": "p00001",
               "bullets": [{"text": "Kabra says yields have peaked.", "paragraphIds": ["p00001"]}]}]
    out = render(topics, panel)
    assert "- Kabra says yields have peaked." in out, "the renderer guessed which Kabra spoke"
    assert "Societe Generale" not in out and "Nomura" not in out


def test_the_renderer_does_not_modify_its_input():
    speakers, topics, _clocks, _points = fixture()
    first = topics[0]["bullets"][0]["text"]
    render(topics, speakers)
    render(topics, speakers)
    assert topics[0]["bullets"][0]["text"] == first
