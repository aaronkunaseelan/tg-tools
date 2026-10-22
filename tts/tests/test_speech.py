"""Speech: chunking and PCM/pace bounds (pure tables), plus the Azure TTS HTTP edge.

chunk_speech_text: never over max_chars, never mid-word; CJK on Azure gets the 900-char limit
_validate_pcm      | empty / odd length / silent / one stray sample -> SpeechError | real tone -> ok
validate_duration_pace | 3369 Han chars in 799 s -> ok | 158 words in 799 s -> audio_pace_implausible
Azure edge (real HTTP) | response                       | outcome
                       | 200 octet-stream PCM           | PCM returned, Accept octet-stream, api-key header
                       | 200 JSON error body            | azure_tts_error "JSON instead of PCM"
                       | 200 text/html                  | azure_tts_error "text/html"
                       | 401 / 429 / 503 / 400          | azure_auth_required / rate_limited / model_transient / invalid_input
"""
import struct

import pytest

from chain_harness import Edge
from tts_common.speech_text import chunk_speech_text
from tts_worker import speech_gemini as sg
from tts_worker.speech_gemini import SpeechError

TONE = struct.pack("<" + "h" * 240, *([1000, -1000] * 120))


@pytest.mark.parametrize("text, max_chars", [
    (" ".join(f"Sentence number {i} ends here." for i in range(200)), 500),
    (("word " * 500).strip(), 100),
    ("梁文锋" * 1000, sg.speech_chunk_char_limit("azure", "梁文锋" * 1000)),
])
def test_chunking(text, max_chars):
    chunks = chunk_speech_text(text, max_chars=max_chars)
    assert all(len(c) <= max_chars and not c[0].isspace() for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")
    assert sg.speech_chunk_char_limit("azure", "ordinary English") == sg.speech_chunk_char_limit("gemini", text) == 3000


@pytest.mark.parametrize("pcm, ok", [(b"", False), (b"\x01\x02\x03", False), (b"\x00\x00" * 2400, False),
                                     (b"\xe8\x03" + b"\x00\x00" * 23999, False), (TONE, True)])
def test_validate_pcm(pcm, ok):
    if ok:
        sg._validate_pcm(pcm, 1, real_media=True)
    else:
        with pytest.raises(SpeechError):
            sg._validate_pcm(pcm, 1, real_media=True)


def test_duration_pace():
    sg.validate_duration_pace("梁文锋" * 1123, 798.77)
    with pytest.raises(SpeechError) as slow:
        sg.validate_duration_pace("word " * 158, 798.77)
    assert slow.value.error_class == "audio_pace_implausible"


@pytest.mark.parametrize("reply, error_class, message", [
    ((200, TONE), None, None),
    ((200, {"error": {"code": "bad_input", "message": "boom"}}), "azure_tts_error", "JSON instead of PCM"),
    ((200, b"<html>upstream failure</html>", "text/html; charset=utf-8"), "azure_tts_error", "text/html"),
    ((401, {"error": "auth"}), "azure_auth_required", None),
    ((429, {"error": "slow down"}), "rate_limited", None),
    ((503, {"error": "busy"}), "model_transient", None),
    ((400, {"error": "bad"}), "invalid_input", None),
])
def test_azure_edge(reply, error_class, message):
    edge = Edge()
    edge.route("POST", r"/openai/v1/audio/speech$", lambda req: reply)
    provider = sg.AzureSpeechProvider(base_url=edge.url, api_key="k")
    try:
        if error_class is None:
            assert provider.synthesize_pcm("hello")[0] == TONE
            (req,) = edge.requests
            assert (req.headers["accept"], req.headers["api-key"], req.json()["response_format"]) == (
                "application/octet-stream", "k", "pcm")
        else:
            with pytest.raises(SpeechError) as err:
                provider.synthesize_pcm("hello")
            assert err.value.error_class == error_class and (message or "") in str(err.value)
    finally:
        edge.close()
