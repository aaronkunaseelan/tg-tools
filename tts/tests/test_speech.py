"""Speech: chunking and PCM/pace bounds (pure tables), plus the Azure TTS HTTP edge.

chunk_speech_text: never over max_chars, never mid-word; CJK on Azure gets the 900-char limit
_validate_pcm      | empty / odd length / silent / one stray sample -> SpeechError | real tone -> ok
validate_duration_pace | 3369 Han chars in 799 s -> ok | 158 words in 799 s -> audio_pace_implausible
Azure edge (real HTTP) | response                       | outcome
                       | 200 octet-stream PCM           | PCM returned, Accept octet-stream, api-key header
                       | 200 JSON error body            | azure_tts_error "JSON instead of PCM"
                       | 200 text/html                  | azure_tts_error "text/html"
                       | 401 / 429 / 503 / 400          | azure_auth_required / rate_limited / model_transient / invalid_input
Self-hosted edge (m3-tts) | 200 PCM -> Bearer token, POST <base>/audio/speech | 401 -> self_hosted_auth_required
                          | 503 -> model_transient | 200 JSON -> self_hosted_tts_error
synthesize_with_fallback  | self-hosted ok      | one read by self-hosted, no notes
                          | self-hosted fails   | Azure reads every chunk again from chunk 1; one note naming the failure
                          | both fail           | Azure's error is raised
"""
import subprocess
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


@pytest.mark.parametrize("reply, error_class", [
    ((200, TONE), None),
    ((401, {"error": "auth"}), "self_hosted_auth_required"),
    ((503, {"error": "busy"}), "model_transient"),
    ((200, {"error": {"message": "kokoro failed"}}), "self_hosted_tts_error"),
])
def test_self_hosted_edge(reply, error_class):
    edge = Edge()
    edge.route("POST", r"/v1/audio/speech$", lambda req: reply)
    provider = sg.SelfHostedSpeechProvider(base_url=edge.url + "/v1", token="t")
    try:
        if error_class is None:
            assert provider.synthesize_pcm("hello")[0] == TONE
            (req,) = edge.requests
            assert (req.path, req.headers["authorization"], req.json()) == (
                "/v1/audio/speech", "Bearer t",
                {"model": "kokoro-82m", "input": "hello", "voice": "af_heart", "response_format": "pcm"})
        else:
            with pytest.raises(SpeechError) as err:
                provider.synthesize_pcm("hello")
            assert err.value.error_class == error_class
    finally:
        edge.close()


def _media(args):
    if args[0] == "ffprobe":
        return subprocess.CompletedProcess(args, 0, stdout='{"format": {"duration": "12"}}', stderr="")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _provider(name, voice, fail_at=None):
    calls = []
    def post(payload):
        calls.append(payload["input"])
        if fail_at is not None and len(calls) >= fail_at:
            raise SpeechError("model_transient", f"{name} down")
        return TONE
    cls = sg.SelfHostedSpeechProvider if name == "self-hosted" else sg.AzureSpeechProvider
    kwargs = {"token": "t"} if name == "self-hosted" else {"api_key": "k"}
    return cls(base_url="https://synthetic.test", voice=voice, http_post=post, **kwargs), calls


@pytest.mark.parametrize("kokoro_fails_at, azure_fails_at, spoke", [(None, None, "self-hosted"), (2, None, "azure"),
                                                                     (1, 1, None)])
def test_fallback_reads_the_whole_episode_with_one_voice(tmp_path, kokoro_fails_at, azure_fails_at, spoke):
    text = "\n\n".join(f"Paragraph {i}. " + "Words in a sentence. " * 100 for i in range(3))
    kokoro, kokoro_calls = _provider("self-hosted", "af_heart", kokoro_fails_at)
    azure, azure_calls = _provider("azure", "nova", azure_fails_at)
    if spoke is None:
        with pytest.raises(SpeechError, match="azure down"):
            sg.synthesize_with_fallback(text, tmp_path / "speech", providers=[kokoro, azure], runner=_media)
        return
    result, provider, notes = sg.synthesize_with_fallback(text, tmp_path / "speech", providers=[kokoro, azure],
                                                          runner=_media)
    chunks = len(chunk_speech_text(text, max_chars=3000))
    assert chunks > 1 and provider.name == spoke and len(result.receipts) == chunks
    assert {(r["provider"], r["model"]) for r in result.receipts} == {
        ("self-hosted", "kokoro-82m") if spoke == "self-hosted" else ("azure", "gpt-4o-mini-tts")}
    if spoke == "self-hosted":
        assert notes == [] and azure_calls == []
    else:
        assert len(azure_calls) == chunks and len(kokoro_calls) == 2
        assert notes == ["speech_fallback: self-hosted failed (model_transient: self-hosted down); "
                         "read again with azure voice nova"]
