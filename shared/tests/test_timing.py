"""Timed transcript (<prefix>.segments.json): when each canonical paragraph was spoken.

episode_enrichment.timing.build: script + timing -> (artifact, manifest summary)
| timing                                         | artifact | paragraph times                     |
| m3 words                                       | yes      | first word's start, last word's end |
| m3 segments, no words                          | yes      | per segment                         |
| Azure chunk texts (10 min each)                | yes      | the chunk span (basis "chunks")     |
| units whose characters differ from the script  | no       | basis null, reason says why         |
| none                                           | no       | basis null, reason                  |
stt.transcribe_audio: self-hosted (text, label, {words, segments}) -> timing basis words;
                      Azure chunk texts -> basis chunks, the last chunk ends at the probed duration.
ensure_script: writes segments.json + timing.json; a resumed job (no STT) publishes the same bytes;
               a script source (reader speech) has no timing. checkpointed_timed_transcript refuses a
               segments.json that belongs to another script. The manifest records the basis and segmentsUrl.
"""
import json
from types import SimpleNamespace

from episode_enrichment import timing
from episode_enrichment.paragraphs import build_stream
from episode_enrichment.pipeline import (TIMED_FILE, checkpointed_timed_transcript, ensure_script)
from episode_enrichment.render import build_manifest
from episode_enrichment.stt import transcribe_audio

SCRIPT = "Good morning everyone.\n\nThis is Bloomberg Surveillance, with Tom and Paul."
WORDS = [{"word": w, "start": float(i), "end": i + 0.8} for i, w in enumerate(
    "Good morning everyone. This is Bloomberg Surveillance , with Tom and Paul.".split())]


def test_words_map_exactly_onto_paragraphs():
    text, summary = timing.build(SCRIPT, timing.from_self_hosted({"words": WORDS, "segments": None}))
    artifact = json.loads(text)
    assert summary == {"basis": "words", "paragraphs": 2, "segments": len(WORDS)}
    assert artifact["paragraphs"] == [{"id": "p00001", "start": 0.0, "end": 2.8},
                                      {"id": "p00002", "start": 3.0, "end": 11.8}]
    assert artifact["streamSha256"] == build_stream(SCRIPT).stream_sha256 and artifact["basis"] == "words"


def test_segments_without_words_and_azure_chunks():
    segments = [{"id": 0, "start": 0.4, "end": 2.9, "text": "Good morning everyone."},
                {"id": 1, "start": 3.1, "end": 12.0, "text": "This is Bloomberg Surveillance, with Tom and Paul."}]
    text, summary = timing.build(SCRIPT, timing.from_self_hosted({"words": None, "segments": segments}))
    assert summary["basis"] == "segments" and json.loads(text)["paragraphs"][1] == {"id": "p00002", "start": 3.1, "end": 12.0}
    chunks = timing.from_chunks(["Good morning everyone. This is", "Bloomberg Surveillance, with Tom and Paul."], 600, 700.5)
    text, summary = timing.build(SCRIPT, chunks)
    assert summary["basis"] == "chunks"
    assert json.loads(text)["paragraphs"] == [{"id": "p00001", "start": 0, "end": 600},
                                              {"id": "p00002", "start": 0, "end": 700.5}]   # spans both chunks


def test_unknown_timing_is_never_a_guess():
    assert timing.build(SCRIPT, timing.from_self_hosted(None)) == (
        None, {"basis": None, "reason": "the self-hosted server sent no timings"})
    text, summary = timing.build(SCRIPT, timing.from_self_hosted({"words": WORDS[:-1], "segments": None}))
    assert text is None and summary["basis"] is None and "do not match the script" in summary["reason"]


def tool(self_hosted):
    return SimpleNamespace(
        _load_local_env=lambda: None, ensure_dependencies=lambda: None,
        load_self_hosted_config=lambda: object(), transcribe_self_hosted=lambda config, path, vocab=None: self_hosted,
        load_azure_openai_config=lambda: None, fetch_azure_access_token=lambda config: "token",
        resolve_transcribe_model=lambda model, config: "chain-mini", prepare_chunks=lambda path, tmp: ["a", "b"],
        transcribe_chunk_texts=lambda config, token, chunks, model, jobs, on_progress: ["Good morning everyone.",
                                                                                         SCRIPT.split("\n\n")[1]],
        CHUNK_DURATION_SEC=600, probe=lambda path: {"format": {"duration": "700.5"}},
        format_transcript=lambda raw: raw)


def test_episode_transcripts_ask_for_no_vocab():
    seen = []
    t = tool(None)
    t.transcribe_self_hosted = lambda config, path, vocab=None: seen.append(vocab)
    transcribe_audio("a.mp3", module=t)
    assert seen == ["none"]


def test_speech_to_text_hands_over_its_timing():
    local = transcribe_audio("a.mp3", module=tool((SCRIPT, "parakeet-ultra@fluidaudio-0.18.0",
                                                   {"words": WORDS, "segments": None})))
    assert local.timing["basis"] == "words" and len(local.timing["units"]) == len(WORDS)
    azure = transcribe_audio("a.mp3", module=tool(None))
    assert azure == SCRIPT and azure.stt_engine is None
    assert [(u["start"], u["end"]) for u in azure.timing["units"]] == [(0, 600), (600, 700.5)]


class Source:
    content_origin, topic_origin, summary_origin = "stt_transcript", "luna_topics", "luna_summary"

    def __init__(self, text, kind="audio"):
        self.text, self.kind = text, kind

    def transcript(self):
        if self.text is None:
            raise AssertionError("a checkpointed script must not be transcribed again")
        return self.text


def timed(text, words=WORDS):
    from episode_enrichment.stt import Transcript
    transcript = Transcript(text)
    transcript.timing = timing.from_self_hosted({"words": words, "segments": None})
    return transcript


def test_ensure_script_keeps_the_timed_transcript_across_a_resume(tmp_path):
    first = ensure_script(work_dir=tmp_path, source=Source(timed(SCRIPT)))
    assert first.timing["basis"] == "words" and (tmp_path / TIMED_FILE).read_text() == first.timed_transcript
    resumed = ensure_script(work_dir=tmp_path, source=Source(None))
    assert (resumed.timed_transcript, resumed.timing) == (first.timed_transcript, first.timing)
    assert checkpointed_timed_transcript(tmp_path) == first.timed_transcript
    reader = ensure_script(work_dir=tmp_path / "speech", source=Source(SCRIPT, kind="script"))
    assert reader.timed_transcript is None and reader.timing == {"basis": None, "reason": "not speech-to-text"}
    untimed = ensure_script(work_dir=tmp_path / "old", source=Source(SCRIPT))
    assert untimed.timed_transcript is None and untimed.timing["reason"] == "speech-to-text returned no timings"
    assert not (tmp_path / "old" / TIMED_FILE).exists()


def test_a_segments_file_from_another_script_is_refused(tmp_path):
    ensure_script(work_dir=tmp_path, source=Source(timed(SCRIPT)))
    other = json.loads((tmp_path / TIMED_FILE).read_text())
    other["scriptSha256"] = "0" * 64
    (tmp_path / TIMED_FILE).write_text(json.dumps(other))
    assert checkpointed_timed_transcript(tmp_path) is None


def test_the_manifest_records_the_basis_and_the_segments_url():
    stream = build_stream(SCRIPT)
    manifest = json.loads(build_manifest(
        stream=stream, analysis={"topics": []}, mentions={"topics": []}, reports={}, title="T",
        timing={"basis": "chunks", "paragraphs": 2, "segments": 2}, segments_url="https://x/p.segments.json"))
    assert manifest["script"]["timing"]["basis"] == "chunks"
    assert manifest["artifacts"]["segmentsUrl"] == "https://x/p.segments.json"
    plain = json.loads(build_manifest(stream=stream, analysis={"topics": []}, mentions={"topics": []},
                                      reports={}, title="T"))
    assert "timing" not in plain["script"] and "segmentsUrl" not in plain["artifacts"]
