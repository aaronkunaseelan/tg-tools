"""Show metadata from the schedule, not from code (rss-audio/surveillance_metadata.py, SV-condensed D14).

| case                                   | proves                                                               |
| the shipped sources                    | each Surveillance source says where its chapters or guest list come  |
|                                        | from; the playlist id and the Omny URL live in config, not in code   |
| the show's date                        | a show is identified by the date its own title carries               |
| chapters                               | the playlist entry for that date, its description's chapter lines    |
| guest list                             | the radio item whose body names the day, its numbered list           |
| a source with no metadata              | none is invented, and no reader is called                            |
| a wrong block                          | a non-https guest-list URL and chapters without a playlist are errors |
"""
import inspect
import json
from pathlib import Path

import pytest

import surveillance_metadata as metadata

DESCRIPTION = """Chapters:
0:00 Stocks Cool, AI Rotation and Rate Risks — Manish Kabra, Societe Generale
12:34 The Vote and the Dollar — Libby Cantrill, PIMCO
"""
OMNY = """<rss version="2.0"><channel>
<item><title>Something Else</title><description>October 31, 2026 &#8212; the week in markets.</description></item>
<item><title>Markets Price in Midterms</title><description><![CDATA[
<p>November 5, 2026</p>
<p>1) Erin Covey, Editor: US House with Cook Political Report, talks about the midterms.</p>
<p>2) Christopher Hodge, Chief US Economist with Natixis, previews the jobs report.</p>
]]></description></item>
</channel></rss>"""
TITLE = "Bloomberg Surveillance TV 11/5/2026"


def atom(*entries):
    body = "".join(
        f"<entry><id>yt:video:{video}</id><title>{title}</title>"
        f"<media:group><media:description>{description}</media:description></media:group></entry>"
        for video, title, description in entries)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:media="http://search.yahoo.com/mrss/">' + body + "</feed>").encode()


def tv_source():
    return metadata.source("surveillance-tv")


def test_each_source_says_where_its_metadata_comes_from():
    tv, radio = tv_source(), metadata.source("surveillance-radio")
    assert tv["showMetadata"] == {"chapters": True}
    assert tv["playlistId"].startswith("PL")
    assert radio["showMetadata"]["guestList"]["from"] == "omny"
    assert radio["showMetadata"]["guestList"]["rssUrl"].startswith("https://www.omnycontent.com/")


def test_no_show_identity_is_hard_coded_in_the_module():
    code = inspect.getsource(metadata)
    assert "omnycontent" not in code and "youtube.com/feeds" not in code
    assert "PLGaYlBJIOoa" not in code and "e73c998e" not in code


def test_a_show_is_identified_by_the_date_its_own_title_carries():
    assert metadata.shown_date(TITLE) == "11/5/2026"
    assert metadata.shown_date("Bloomberg Surveillance Radio 11/4/2026") == "11/4/2026"
    with pytest.raises(ValueError, match="no show date"):
        metadata.shown_date("Bloomberg Surveillance")


def test_the_chapters_are_the_entry_for_that_date():
    feed = atom(("wrong1", "Bloomberg Surveillance 11/4/2026", DESCRIPTION),
                ("right2", "Bloomberg Surveillance 11/5/2026", DESCRIPTION),
                ("other3", "Bloomberg Surveillance: The Open 11/5/2026", DESCRIPTION))
    chapters, video = metadata.chapter_list(feed, tv_source(), "11/5/2026")
    assert video == "yt:video:right2"
    assert [(c["clock"], c["t"]) for c in chapters] == [("0:00", 0), ("12:34", 754)]
    assert chapters[1]["title"] == "The Vote and the Dollar — Libby Cantrill, PIMCO"


def test_a_playlist_with_no_entry_for_the_date_has_no_chapters():
    feed = atom(("wrong1", "Bloomberg Surveillance 11/4/2026", DESCRIPTION))
    assert metadata.chapter_list(feed, tv_source(), "11/5/2026") == ([], None)


def test_the_playlist_it_reads_is_the_one_the_source_names():
    asked = []

    def playlist(playlist_id):
        asked.append(playlist_id)
        return atom(("right2", "Bloomberg Surveillance 11/5/2026", DESCRIPTION))

    source = {**tv_source(), "playlistId": "PL-from-the-config"}
    found = metadata.show_metadata(source, TITLE, playlist=playlist)
    assert asked == ["PL-from-the-config"]
    assert found["video_id"] == "yt:video:right2" and len(found["chapters"]) == 2


def test_the_guest_list_is_the_item_that_names_the_day():
    guests, item = metadata.guest_list(OMNY, "11/5/2026")
    assert item == "Markets Price in Midterms"
    assert guests == ["Erin Covey, Editor: US House with Cook Political Report, talks about the midterms.",
                      "Christopher Hodge, Chief US Economist with Natixis, previews the jobs report."]
    assert metadata.guest_list(OMNY, "11/6/2026") == ([], None)


def test_the_radio_shows_guest_list_comes_from_the_url_the_source_names():
    asked = []

    def get(url, headers=None):
        asked.append((url, headers))
        return OMNY.encode()

    source = {**metadata.source("surveillance-radio"), "title": "Bloomberg Surveillance Radio 11/5/2026"}
    found = metadata.show_metadata(source, source["title"], get=get)
    assert asked[0][0] == source["showMetadata"]["guestList"]["rssUrl"]
    assert len(found["guest_list"]) == 2 and found["chapters"] == []
    assert found["guest_item"] == "Markets Price in Midterms"


def test_a_source_with_no_metadata_has_none_and_reads_nothing():
    def never(*args, **kwargs):
        raise AssertionError("a source with no showMetadata must not read anything")

    assert metadata.show_metadata({"id": "some-show"}, TITLE, get=never, playlist=never) == {}


def test_a_guest_list_url_that_is_not_https_is_an_error():
    source = {"id": "surveillance-radio",
              "showMetadata": {"guestList": {"rssUrl": "http://www.omnycontent.com/x/podcast.rss"}}}
    with pytest.raises(ValueError, match="must be an https URL"):
        metadata.show_metadata(source, TITLE, get=lambda *a, **k: b"")


def test_chapters_asked_of_a_source_with_no_playlist_is_an_error():
    source = {"id": "surveillance-radio", "showMetadata": {"chapters": True}}
    with pytest.raises(ValueError, match="names no playlistId"):
        metadata.show_metadata(source, TITLE, playlist=lambda *a: b"")


def test_a_source_that_is_not_scheduled_is_not_found():
    with pytest.raises(KeyError, match="no scheduled source"):
        metadata.source("surveillance-atlantis")


def test_the_inventory_it_reads_is_the_schedules(tmp_path):
    inventory = json.loads((Path(__file__).resolve().parents[2] / "config" /
                            "scheduled-sources.json").read_text())
    inventory["sources"] = [s for s in inventory["sources"] if s["id"] == "surveillance-tv"]
    path = tmp_path / "scheduled-sources.json"
    path.write_text(json.dumps(inventory))
    assert [s["id"] for s in metadata.sources(path)] == ["surveillance-tv"]
    assert metadata.source("surveillance-tv", path)["showMetadata"] == {"chapters": True}
