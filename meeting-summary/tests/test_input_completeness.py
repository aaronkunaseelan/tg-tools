"""Meeting processing requires complete audio, text and provider completion."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import audio
import summarizer
import pytest


@pytest.mark.parametrize("terminal", [
    b'', b'data: [DONE]\\n',
    b'data: {"type":"response.incomplete"}\\n',
    b'data: {"type":"error"}\\n',
])
def test_provider_completion_is_required(terminal):
    stream = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"{}"}\\n' + terminal)
    with pytest.raises(summarizer.SummaryError):
        summarizer.read_sse_output_text(stream)


def test_long_transcript_is_split_without_losing_text(monkeypatch):
    text = 'a' * 16000 + 'b' * 16000 + 'FINAL DECISION'
    prompts = []
    monkeypatch.setattr(summarizer, 'load_openai_codex_auth', lambda path: object())
    def call(auth, **kwargs):
        prompts.append(kwargs['prompt'])
        return {}
    monkeypatch.setattr(summarizer, 'call_codex_json', call)
    summaries, final = summarizer.summarize_transcript_chunks(
        settings=NS(pi_auth_path=None, summary_model='synthetic', summary_timeout_sec=1),
        chunk_texts=[text])
    assert len(summaries) == 3
    for index in range(3):
        assert text[index * 16000:(index + 1) * 16000] in prompts[index]
    assert 'FINAL DECISION' in prompts[2]


class MeetingCompleteness(unittest.TestCase):
    def test_audio_tail_failure_must_not_return_a_partial_success(self):
        settings = NS(max_audio_duration_sec=7200, chunk_seconds=600,
                      ffprobe_bin='fake-ffprobe', ffmpeg_bin='fake-ffmpeg')
        def run(argv, **kwargs):
            dest = Path(argv[-1])
            dest.write_bytes(b'x' * (16 if dest.name == 'chunk-001.wav' else 2048))
            return NS(returncode=0, stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(audio, 'probe_duration', return_value=1200), \
                 patch.object(audio.subprocess, 'run', side_effect=run):
                with self.assertRaises(audio.AudioError):
                    result = audio.prepare_audio(Path(tmp) / 'input', Path(tmp), settings)
                    self.assertEqual(len(result.chunk_paths), 1)

    def test_summary_must_not_drop_the_end_of_a_chunk(self):
        sentinel = 'SYNTHETIC_FINAL_DECISION'
        text = 'x' * 17000 + sentinel
        prompt = summarizer.build_chunk_summary_prompt(0, 1, text)
        self.assertTrue(sentinel in prompt,
                        'summary discards source after character 15997')

    def test_failed_response_stream_must_not_be_success(self):
        stream = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"{}"}\n'
                            b'data: {"type":"response.failed","response":{"status":"failed"}}\n')
        with self.assertRaises(summarizer.SummaryError):
            summarizer.read_sse_output_text(stream)


