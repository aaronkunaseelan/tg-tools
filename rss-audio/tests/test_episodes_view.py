"""rss-audio-episodes (episodes_view.py): grouping, kinds, versions, clocks, order, failures (fake lister only).

The listing fixture is real object names, copied from a read-only
``b2 ls b2://<bucket>/surveillance/episodes/`` and
``b2 ls b2://<bucket>/audio/episodes/`` on 11 Nov 2026 (names only, no sizes and no keys):
a condensed pair, an episode whose documents were rebuilt at v2, and an ordinary episode from another feed.

| case            | proves                                                                                |
| grouping        | objects sharing a stem are one episode; the condensed edition is its own entry         |
| parent          | a condensed entry names the full show it was cut from                                   |
| kinds           | the suffix map names all nine real suffixes; anything else lands under `other`          |
| versions        | a `.v<n>.` name sets that kind to n (mp3 stays v1: the audio is never versioned)        |
| clocks          | condensed-audio with segments, audio without one, none when there are no segments       |
| order/limit     | newest first, undated last, parent before its edition, `--limit` caps what is listed    |
| date            | `--date` keeps one day; a day with nothing is one line and exit 2                       |
| unknown         | a feed name that is not a prefix, or an empty prefix: one line, exit 2                  |
| listing failure | an unreadable listing is `unknown: <reason>`, exit 1, never an empty table               |
| read only       | one `b2 ls` on `<feed>/episodes/`, nothing else                                        |
| redaction       | no URL and no 28+ character token is printed, in the table or in `--json`               |
"""
import io
import json
import subprocess
from pathlib import Path

import episodes_view as ev
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "listing" / "b2-ls-episodes.txt"


@pytest.fixture(autouse=True)
def bucket(monkeypatch):
    """The bucket comes from the environment in production; tests name an example one."""
    monkeypatch.setattr(ev, "BUCKET", "example-bucket")
TV = "2026-11-09-bloomberg-surveillance-tv-4b8b8be4b7ba"
RADIO = "2026-10-28-bloomberg-surveillance-radio-fbb058df0716"
AUDIO = "2026-10-22-mts-flapping-airplanes-1b-series-36e4838063f4"
TOKEN = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7"     # 34 characters: never printed whole (a fake fixture)  gitleaks:allow
URL = "https://edge.example/x.mp3"


def listing() -> list[str]:
    return FIXTURE.read_text().splitlines()


class Lister:
    """Stands in for ``b2 ls``: never a network, never a key."""

    def __init__(self, lines=None, fail=None):
        self.lines, self.fail, self.calls = listing() if lines is None else list(lines), fail, []

    def __call__(self, feed):
        self.calls.append(feed)
        if self.fail:
            raise ev.Listing(self.fail)
        head = f"{feed}/episodes/"
        return [name for name in self.lines if name.startswith(head)]


def run(argv, lister=None):
    out = io.StringIO()
    code = ev.main(argv, lister=lister if lister is not None else Lister(), out=out)
    return code, out.getvalue()


def episodes(feed="surveillance", lister=None) -> list[dict]:
    return ev.collect(feed, lister=lister if lister is not None else Lister())["episodes"]


def one(name, feed="surveillance", lister=None) -> dict:
    return next(episode for episode in episodes(feed, lister) if episode["name"] == name)


def test_grouping_makes_one_episode_per_stem():
    found = episodes()
    assert [episode["name"] for episode in found] == [TV, TV + "-condensed", RADIO]
    assert {episode["date"] for episode in found} == {"2026-11-09", "2026-10-28"}
    assert all(episode["condensed"] is False for episode in (one(TV), one(RADIO)))


def test_condensed_edition_names_its_parent():
    condensed = one(TV + "-condensed")
    assert condensed["condensed"] is True
    assert condensed["parent"] == TV
    assert condensed["kinds"] == ["mp3", "script", "segments", "notes", "manifest", "page", "chapters", "plan",
                                 "structure"]
    assert condensed["other"] == []


def test_kinds_are_the_suffix_map_and_the_audio_first():
    assert one(TV)["kinds"] == ["mp3", "script", "segments"]        # the parent: audio, script, timings
    assert one(AUDIO, "audio")["kinds"] == ["mp3", "script", "notes", "manifest"]


def test_versions_are_the_highest_per_kind_and_the_audio_is_never_versioned():
    rebuilt = one(RADIO)
    assert rebuilt["versions"] == {"mp3": 1, "script": 2, "segments": 2, "notes": 2, "manifest": 2, "page": 2}
    assert rebuilt["kinds"] == ["mp3", "script", "segments", "notes", "manifest", "page"]


def test_an_ordinary_episode_from_another_feed_has_no_condensed_parent():
    ordinary = one(AUDIO, "audio")
    assert ordinary["condensed"] is False and ordinary["parent"] is None
    assert ordinary["date"] == "2026-10-22"


@pytest.mark.parametrize("name, condensed, kinds, clock", [
    (TV, False, ["mp3", "script", "segments"], "audio"),
    (TV + "-condensed", True, ["mp3", "script", "segments"], "condensed-audio"),
    (AUDIO, False, ["mp3", "script", "notes", "manifest"], "none"),
])
def test_clocks(name, condensed, kinds, clock):
    episode = one(name, "audio" if name == AUDIO else "surveillance")
    assert (episode["condensed"], episode["kinds"][:len(kinds)], episode["clocks"]) == (condensed, kinds, clock)


def test_an_unclassified_suffix_is_listed_under_other_and_makes_no_episode():
    # `.input.json` is the publisher's own job-input document (rss-audio/tests/fixtures/surveillance/*.input.json);
    # the live listing carries no suffix the map does not know, so this one object is added to it.
    extra = f"surveillance/episodes/{TV}-condensed.input.json"
    lister = Lister(listing() + [extra])
    found = episodes(lister=lister)
    assert len(found) == 3
    assert one(TV + "-condensed", lister=lister)["other"] == [".input.json"]
    assert one(TV + "-condensed", lister=lister)["kinds"][0] == "mp3"
    assert all(episode["name"] != TV + "-condensed.input" for episode in found)


def test_newest_first_parent_before_its_edition_and_undated_last():
    lines = [f"audio/episodes/{AUDIO}.mp3",
             "audio/episodes/undated-mp3-telegram-dde7ab17210a.mp3",
             "audio/episodes/2026-10-27-builders-kaleidoscope-builders-dario-daniela-amodei-7154109155df.mp3",
             "audio/episodes/2026-11-09-bloomberg-surveillance-tv-4b8b8be4b7ba-condensed.mp3",
             "audio/episodes/2026-11-09-bloomberg-surveillance-tv-4b8b8be4b7ba.mp3"]
    names = [episode["name"] for episode in ev.group(ev.bare(line, "audio") for line in lines)]
    assert names == ["2026-11-09-bloomberg-surveillance-tv-4b8b8be4b7ba",
                     "2026-11-09-bloomberg-surveillance-tv-4b8b8be4b7ba-condensed",
                     "2026-10-27-builders-kaleidoscope-builders-dario-daniela-amodei-7154109155df",
                     AUDIO, "undated-mp3-telegram-dde7ab17210a"]


def test_the_table_caps_at_limit_and_counts_the_rest():
    code, text = run(["surveillance", "--limit", "2"])
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "rss-audio episodes: surveillance — 2 of 3 under surveillance/episodes/ (newest first)"
    assert len(lines) == 3
    assert TV + "-condensed" in lines[2] and "of " + TV in lines[2] and "condensed-audio" in lines[2]


def test_the_table_shows_a_version_above_one_as_at_n():
    code, text = run(["surveillance"])
    assert code == 0
    rebuilt = next(line for line in text.splitlines() if RADIO in line)
    assert "mp3 script@2 segments@2" in rebuilt


def test_date_keeps_one_day():
    code, text = run(["surveillance", "--date", "2026-11-09"])
    assert (code, len(text.splitlines())) == (0, 3)
    assert "on 2026-11-09" in text.splitlines()[0]
    assert all("2026-10-28" not in line for line in text.splitlines())
    code, text = run(["surveillance", "--date", "2026-11-08"])
    assert code == 2 and text == "unknown: nothing stored under surveillance/episodes/ on 2026-11-08\n"


def test_an_empty_prefix_is_one_line_and_exit_2():
    code, text = run(["skeptic-tank"], lister=Lister(listing()))
    assert (code, text.count("\n"), text) == (2, 1, "unknown: nothing stored under skeptic-tank/episodes/\n")


@pytest.mark.parametrize("feed", ["Bad/Feed", "../etc", "", "surveillance/episodes"])
def test_a_name_that_is_not_a_feed_prefix_never_lists(feed):
    lister = Lister()
    code, text = run([feed], lister=lister)
    assert (code, text.count("\n")) == (2, 1)
    assert text.startswith("unknown: ") and lister.calls == []


def test_an_unreadable_listing_is_reported_not_empty():
    code, text = run(["surveillance"], lister=Lister(fail="b2 ls exited 1"))
    assert (code, text) == (1, "unknown: b2 ls exited 1\n")


def test_list_objects_runs_one_read_only_listing(monkeypatch):
    seen = []
    done = subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{TV}.mp3\n{TV}.script.md\n", stderr="")

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return done
    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    assert ev.list_objects("surveillance") == [f"{TV}.mp3", f"{TV}.script.md"]
    assert seen == [(["b2", "ls", "b2://example-bucket/surveillance/episodes/"],
                     {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "check": False,
                      "timeout": ev.TIMEOUT})]


def test_a_failed_listing_names_the_reason_and_no_url(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout="",
                                           stderr="ERROR: cannot reach " + URL)
    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    with pytest.raises(ev.Listing) as failure:
        ev.list_objects("surveillance")
    assert "http" not in str(failure.value)


def test_nothing_printed_holds_a_url_or_a_long_token():
    lines = [f"audio/episodes/{TOKEN}.mp3", f"audio/episodes/{URL}"]
    code, text = run(["audio"], lister=Lister(lines))
    assert code == 0
    assert TOKEN not in text and TOKEN[:8] in text
    assert "http" not in text and "<url>" in text
    code, data = run(["audio", "--json"], lister=Lister(lines))
    assert code == 0
    assert TOKEN not in data and "http" not in data
    assert json.loads(data)["schema"] == "rss-audio.episodes/1"


def test_no_bucket_is_an_unknown_not_a_default(monkeypatch):
    monkeypatch.setattr(ev, "BUCKET", "")
    with pytest.raises(ev.Listing, match="no bucket"):
        ev.list_objects("surveillance")
