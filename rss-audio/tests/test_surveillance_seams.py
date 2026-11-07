"""Surveillance seams (rss-audio/surveillance_seams.py) on compact fixtures shaped like the study's shows.

Each fixture is speech every 30 s plus the cue sentences at the study's offsets (no full transcripts).
The rule itself was checked on the six real m3 transcripts (5 radio days, TV 11/3): 12 of 12 cuts on
the study's paragraphs, through timing.build's segments.json (supervisor work/SV/RECEIPT-step4.md).

| fixture                              | proves                                                               |
| radio, re-intro 5 s before the hour  | the unconfirmed line is rejected; cut at the confirmed open (seam)   |
| radio, 3 Nov and 30 Oct              | labels on New York's wall clock across the DST change (EST, EDT)     |
| TV, hour 3 open without the hosts    | confirmed by "audience worldwide" within 30 s; a bare line 45 s      |
|                                      | earlier is rejected; the cut is the montage start after the silence  |
| TV, no hour-2 open                   | hour 2 falls back to the paragraph nearest the mark                  |
| no timings                           | marks by character share; rule estimated                             |
| through the hook                     | enrich's check_segments accepts the segments (cover once, in order)  |
"""
import json

from episode_enrichment import timing
from episode_enrichment.segments import check_segments
from episode_enrichment.paragraphs import build_stream

from surveillance_seams import SEGMENTERS

RADIO, TV = SEGMENTERS["surveillance-radio"], SEGMENTERS["surveillance-tv"]
RADIO_OPEN = "This is Bloomberg Surveillance with Tom Keene and Paul Sweeney."
RADIO_TAIL = "Bloomberg Surveillance on Bloomberg Radio, Bloomberg Originals and the Bloomberg Business app."
REINTRO = "This is Bloomberg Surveillance with Tom Keene and Paul Sweeney on Bloomberg Radio. Good morning, everyone."
TV_OPEN = "This is Bloomberg Surveillance with Jonathan Ferro, Lisa Abramowicz and Annmarie Hordern."
TV_BARE = "This is Bloomberg Surveillance. Good morning."
TV_HOUR3 = "Live from New York City, for our audience worldwide, the third hour of Bloomberg Surveillance starts now."


def show(duration, cues, silences=()):
    """Speech every 30 s, the cues at their times; a silence (end, start) leaves a gap before a time."""
    lines = {float(t): f"Market talk number {t}." for t in range(0, int(duration), 30)}
    for t, text in cues:
        lines = {k: v for k, v in lines.items() if abs(k - t) >= 10}
        lines[float(t)] = text
    starts = sorted(lines)
    units = []
    for i, t in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else duration
        end = min(t + 8, nxt - 1)
        for before, gap in silences:
            if t < before <= nxt:
                end = min(end, before - gap)
        units.append({"start": t, "end": end, "text": lines[t]})
    script = "\n\n".join(u["text"] for u in units)
    text, _ = timing.build(script, {"basis": "segments", "units": units, "segments": units})
    stream = build_stream(script)
    assert len(stream.paragraphs) == len(units)
    return stream, json.loads(text)


def at(stream, timed, seconds):
    """The id of the paragraph that starts at ``seconds``."""
    return next(p["id"] for p in timed["paragraphs"] if p["start"] == seconds)


def radio_day():
    cues = [(29, RADIO_OPEN), (69, RADIO_TAIL), (3595, REINTRO), (3629, RADIO_OPEN), (3669, RADIO_TAIL),
            (7229, RADIO_OPEN), (7269, RADIO_TAIL)]
    return show(10800, cues, silences=[(3629, 20), (7229, 20)])


def test_radio_cuts_at_the_confirmed_opens_and_rejects_the_reintro():
    stream, timed = radio_day()
    segments = RADIO(stream, timed, {"durationSeconds": 10800, "start": "2026-11-03T12:00:00Z",
                                     "timezone": "America/New_York"})
    assert [s.start_paragraph_id for s in segments[1:]] == [at(stream, timed, 3629), at(stream, timed, 7229)]
    assert [s.rule for s in segments] == ["start", "seam", "seam"]
    assert [s.label for s in segments] == ["Hour 1 · 07:00–08:00 EST", "Hour 2 · 08:00–09:00 EST",
                                           "Hour 3 · 09:00–10:00 EST"]
    assert segments[1].start_s == 3629 and segments[0].end_s < 3629 and not check_segments(stream, segments)


def test_radio_labels_follow_daylight_time():
    stream, timed = radio_day()
    segments = RADIO(stream, timed, {"durationSeconds": 10800, "start": "2026-10-30T11:00:00Z",
                                     "timezone": "America/New_York"})
    assert segments[0].label == "Hour 1 · 07:00–08:00 EDT" and segments[2].label == "Hour 3 · 09:00–10:00 EDT"


def tv_day(hour2=True):
    duration = 8640   # marks at 2880 and 5760
    cues = [(26, TV_OPEN), (5770, TV_BARE), (5815, "This is Bloomberg Surveillance."), (5825, TV_HOUR3),
            (5793, "We are going to see a rate cut."), (5803, "Markets are higher.")]
    silences = [(5793, 8)]
    if hour2:
        cues += [(2903, "The bond market is telling you something."), (2913, "We are in a bubble."), (2925, TV_OPEN)]
        silences.append((2903, 8))
    return show(duration, cues, silences)


def test_tv_cuts_at_the_montage_before_a_confirmed_open():
    stream, timed = tv_day()
    segments = TV(stream, timed, {"durationSeconds": 8640})
    assert [s.start_paragraph_id for s in segments[1:]] == [at(stream, timed, 2903), at(stream, timed, 5793)]
    assert [s.rule for s in segments] == ["start", "seam", "seam"]
    assert segments[1].label == "Hour 2 · 0:48–1:37" and segments[2].label.startswith("Hour 3 · 1:37–")
    assert not check_segments(stream, segments)


def test_tv_without_an_hour_open_falls_back_to_the_mark():
    stream, timed = tv_day(hour2=False)
    segments = TV(stream, timed, {"durationSeconds": 8640})
    assert [s.rule for s in segments] == ["start", "fallback", "seam"]
    assert segments[1].start_paragraph_id == at(stream, timed, 2880)


def test_without_timings_the_marks_are_estimated():
    stream, timed = radio_day()
    segments = RADIO(stream, None, {"durationSeconds": 10800})
    assert [s.rule for s in segments] == ["start", "estimated", "estimated"]
    assert [s.label for s in segments] == ["Hour 1", "Hour 2", "Hour 3"] and not check_segments(stream, segments)
    ids = [p.id for p in stream.paragraphs]
    third = ids.index(segments[1].start_paragraph_id) / len(ids)
    assert 0.30 < third < 0.37
