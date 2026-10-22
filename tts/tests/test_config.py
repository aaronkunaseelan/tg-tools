"""Account configuration is explicit; notification output uses the feed title."""

import pytest

from tts_worker.config import load_settings
from tts_worker.result_projector import render_notification

ACCOUNT_ENV = {
    "TTS_B2_BUCKET": "example-audio",
    "TTS_FEED_URL": "https://rss.example.test/tts.xml",
    "TTS_FEED_TITLE": "Example TTS",
    "TTS_AZURE_TTS_BASE_URL": "https://speech.example.test/",
    "TTS_AZURE_TTS_KEY_CMD": "printf example-speech-key",
    "TTS_AZURE_CHAT_BASE_URL": "https://chat.example.test/openai/v1/",
    "TTS_AZURE_CHAT_KEY_CMD": "printf example-chat-key",
}


@pytest.mark.parametrize("key", ACCOUNT_ENV)
@pytest.mark.parametrize("missing", [None, "", "   "])
def test_publication_requires_config(key, missing):
    env = dict(ACCOUNT_ENV)
    if missing is None:
        del env[key]
    else:
        env[key] = missing
    with pytest.raises(ValueError, match=key):
        load_settings(env)


def test_publication_config_and_notification(monkeypatch):
    settings = load_settings(ACCOUNT_ENV)
    assert settings.b2_bucket == ACCOUNT_ENV["TTS_B2_BUCKET"]
    assert settings.feed_url == ACCOUNT_ENV["TTS_FEED_URL"]
    assert settings.feed_title == ACCOUNT_ENV["TTS_FEED_TITLE"]
    assert settings.azure_tts_base_url == ACCOUNT_ENV["TTS_AZURE_TTS_BASE_URL"].rstrip("/")
    assert settings.azure_chat_base_url == ACCOUNT_ENV["TTS_AZURE_CHAT_BASE_URL"].rstrip("/")
    assert settings.azure_tts_key_cmd == ACCOUNT_ENV["TTS_AZURE_TTS_KEY_CMD"]
    assert settings.azure_chat_key_cmd == ACCOUNT_ENV["TTS_AZURE_CHAT_KEY_CMD"]
    monkeypatch.setenv("TTS_FEED_TITLE", settings.feed_title)
    assert render_notification("succeeded", {"ok": True, "title": "Title", "publicAudioUrl": "audio",
                                           "scriptUrl": "script", "notesUrl": "notes", "feedUrl": "feed"}) == (
        "Posted to Example TTS:\nTitle\naudio\nScript: script\nNotes: notes\nFeed: feed")


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_notification_requires_feed_title(monkeypatch, missing):
    if missing is None:
        monkeypatch.delenv("TTS_FEED_TITLE", raising=False)
    else:
        monkeypatch.setenv("TTS_FEED_TITLE", missing)
    with pytest.raises(ValueError, match="TTS_FEED_TITLE"):
        render_notification("succeeded", {"ok": True})
