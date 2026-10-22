"""transcribe.py pure table: deployment choice and transcript formatting.

| requested model            | env default       | deployment        |
| gpt-4o-mini-transcribe     | -                 | transcribe-mini   |
| gpt-4o-transcribe          | -                 | transcribe-full   |
| ""                         | -                 | transcribe-mini   |
| ""                         | transcribe-full   | transcribe-full   |
| custom-deployment          | -                 | custom-deployment |
format_transcript: spacing before punctuation fixed, long text split into paragraphs, unpunctuated text wrapped
"""
import importlib.util
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
