"""transcribe.py pure table: deployment choice and transcript formatting.

| requested model            | env default       | deployment        |
| gpt-4o-mini-transcribe     | -                 | transcribe-mini   |
| gpt-4o-transcribe          | -                 | transcribe-full   |
| ""                         | -                 | transcribe-mini   |
| ""                         | transcribe-full   | transcribe-full   |
| custom-deployment          | -                 | custom-deployment |
format_transcript: spacing before punctuation fixed, long text split into paragraphs, unpunctuated text wrapped
self-hosted: no URL -> None | whole file posted with model + bearer -> (text, model@engine)
             | HTTP 500, unreachable, over the size cap -> None (the caller falls back to Azure)
"""
import http.server
import importlib.util
import json
import threading
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("transcribe_audio_module", Path(__file__).parents[1] / "transcribe.py")
transcribe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transcribe
SPEC.loader.exec_module(transcribe)
CONFIG = transcribe.AzureOpenAIConfig(endpoint="https://example.invalid", api_version="2024-10-21",
                                      mini_deployment="transcribe-mini", full_deployment="transcribe-full",
                                      tenant_id="t", client_id="c", client_secret="s")


@pytest.mark.parametrize("model, default, deployment", [
    ("gpt-4o-mini-transcribe", None, "transcribe-mini"), ("gpt-4o-transcribe", None, "transcribe-full"),
    ("", None, "transcribe-mini"), ("", "transcribe-full", "transcribe-full"),
    ("custom-deployment", None, "custom-deployment")])
def test_resolve_transcribe_model(monkeypatch, model, default, deployment):
    monkeypatch.delenv(transcribe.TRANSCRIBE_AUDIO_MODEL_ENV, raising=False)
    if default:
        monkeypatch.setenv(transcribe.TRANSCRIBE_AUDIO_MODEL_ENV, default)
    assert transcribe.resolve_transcribe_model(model, CONFIG) == deployment


def test_format_transcript():
    sentences = ["First sentence has extra spaces before punctuation !"] + [
        f"Sentence {n} is long enough to keep the paragraph growing and readable for a listener." for n in range(4)]
    formatted = transcribe.format_transcript("  ".join(sentences))
    assert "punctuation!" in formatted and len(formatted.split("\n\n")) == 2
    assert len(transcribe.format_transcript("word " * 220).split("\n\n")) == 3


class SelfHosted(http.server.BaseHTTPRequestHandler):
    seen: dict = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        SelfHosted.seen = {"path": self.path, "auth": self.headers["Authorization"], "body": body}
        code, reply = (500, {"error": "down"}) if b"fail" in body else (
            200, {"text": " Hello there. ", "model": "parakeet-ultra", "engine": "fluidaudio-0.18.0"})
        data = json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_self_hosted(monkeypatch, tmp_path):
    monkeypatch.delenv(transcribe.TRANSCRIBE_AUDIO_SELF_HOSTED_URL_ENV, raising=False)
    assert transcribe.load_self_hosted_config() is None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SelfHosted)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(transcribe.TRANSCRIBE_AUDIO_SELF_HOSTED_URL_ENV, f"http://127.0.0.1:{server.server_port}/v1/")
    monkeypatch.setenv(transcribe.TRANSCRIBE_AUDIO_SELF_HOSTED_TOKEN_ENV, "tok")
    monkeypatch.delenv(transcribe.TRANSCRIBE_AUDIO_SELF_HOSTED_MODEL_ENV, raising=False)
    monkeypatch.setattr(transcribe, "SELF_HOSTED_TIMEOUT_SEC", 5)
    config = transcribe.load_self_hosted_config()
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00\xffaudio")
    try:
        assert transcribe.transcribe_self_hosted(config, str(audio)) == ("Hello there.", "parakeet-ultra@fluidaudio-0.18.0")
        assert (SelfHosted.seen["path"], SelfHosted.seen["auth"]) == ("/v1/audio/transcriptions", "Bearer tok")
        assert b'name="model"\r\n\r\nparakeet-ultra\r\n' in SelfHosted.seen["body"]
        assert b"\r\n\x00\xffaudio\r\n" in SelfHosted.seen["body"]
        audio.write_bytes(b"fail")
        assert transcribe.transcribe_self_hosted(config, str(audio)) is None
    finally:
        server.shutdown()
        server.server_close()
    assert transcribe.transcribe_self_hosted(config, str(audio)) is None  # server gone
    monkeypatch.setattr(transcribe, "SELF_HOSTED_MAX_BYTES", 1)
    assert transcribe.transcribe_self_hosted(config, str(audio)) is None
