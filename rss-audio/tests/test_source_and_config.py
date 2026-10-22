"""rss-audio pure tables: source input, subprocess hygiene, naming, settings validation.

validate_source_url: metadata IP / localhost / private literal -> SourceAudioError(stage input)
classify_text      | "listen https://x/file.mp3" -> direct_audio_url | "watch https://youtu.be/abc?si=x," -> url (punctuation trimmed)
stable_url_for_display drops tracking; episode names drop stop words, date prefix, 12-hex suffix
sanitized_source_subprocess_env keeps PATH only; yt-dlp never loads remote components (local or via Mac)
first_actionable_error_line skips route wrappers; run_command keeps pipefail failures (real bash)
validate_settings  | complete -> ok | missing / https / scheme-less proxy, missing ssh, relative yt-dlp -> named error
                   | all three missing -> all three named together
"""
import os
from datetime import UTC, datetime
from unittest import mock

import pytest

import source_audio
from config import load_settings, validate_settings
from pipeline import EpisodeMetadata, _episode_names
from source_audio import classify_text, stable_url_for_display

ENV = {"RSS_AUDIO_BASE_URL": "https://rss.example.test", "RSS_AUDIO_B2_BUCKET": "example-audio",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_MULLVAD_PROXY": "http://127.0.0.1:8888",
       "RSS_AUDIO_MAC_SSH": "user@example.invalid", "RSS_AUDIO_MAC_YTDLP_PATH": "/opt/homebrew/bin/yt-dlp"}
TOOLS = source_audio.SourceToolSettings(mac_ssh_target="mac", mac_ytdlp_path="/opt/homebrew/bin/yt-dlp",
                                        yt_dlp_bin="yt-dlp", ffmpeg_bin="ffmpeg", mullvad_proxy="http://proxy:8888")


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://localhost:8000/a.mp3",
                                 "http://10.0.0.5/a.mp3"])
def test_private_source_urls_rejected(url):
    with pytest.raises(source_audio.SourceAudioError) as err:
        source_audio.validate_source_url(url)
    assert err.value.stage == "input"


def test_text_and_naming():
    assert classify_text("listen https://example.com/file.mp3") == ("direct_audio_url", "https://example.com/file.mp3")
    assert classify_text("watch https://youtu.be/abc123?si=xyz,") == ("url", "https://youtu.be/abc123?si=xyz")
    assert stable_url_for_display("https://www.youtube.com/watch?v=abc123&utm_source=foo&si=bar") == \
        "https://www.youtube.com/watch?v=abc123"
    metadata = EpisodeMetadata("TBPN | Travis K discusses his new company Atoms", "", "", 0, "Unknown")
    slug = _episode_names(metadata, "a" * 64, datetime(2026, 3, 21, tzinfo=UTC)).prefix
    assert slug.startswith("2026-03-21-tbpn-travis-atoms-") and len(slug.rsplit("-", 1)[1]) == 12


def test_subprocess_hygiene():
    with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "RSS_AUDIO_BOT_TOKEN": "s", "OPENAI_API_KEY": "s",
                                      "AZURE_CLIENT_SECRET": "s"}, clear=True):
        assert source_audio.sanitized_source_subprocess_env() == {"PATH": "/usr/bin"}
    assert "--no-remote-components" in source_audio._local_metadata_command(TOOLS, "https://example.com/v")
    assert "--no-remote-components" in source_audio._mac_metadata_command(TOOLS, "https://example.com/v")[2]
    assert source_audio.first_actionable_error_line(
        "[OCI probe] Source unavailable: extractor reports this media is not available\n\n"
        "ERROR: [youtube] abc123: This video is not available\n") == "ERROR: [youtube] abc123: This video is not available"
    with pytest.raises(source_audio.SourceAudioError) as err:
        source_audio.run_command(["/bin/bash", "-lc", "set -o pipefail; false | true"], stage="download")
    assert err.value.stage == "download"


@pytest.mark.parametrize("key", ["RSS_AUDIO_BASE_URL", "RSS_AUDIO_B2_BUCKET"])
@pytest.mark.parametrize("missing", [None, "", "   "])
def test_publication_requires_config(key, missing):
    with pytest.raises(ValueError, match=key):
        settings(**{key: missing})


def settings(**change):
    env = {k: v for k, v in {**ENV, **change}.items() if v is not None}
    with mock.patch.dict(os.environ, env, clear=True):
        return load_settings()


@pytest.mark.parametrize("change, named", [
    ({}, ()),
    ({"RSS_AUDIO_MULLVAD_PROXY": None}, ("RSS_AUDIO_MULLVAD_PROXY",)),
    ({"RSS_AUDIO_MULLVAD_PROXY": "https://127.0.0.1:8888"}, ("RSS_AUDIO_MULLVAD_PROXY",)),
    ({"RSS_AUDIO_MULLVAD_PROXY": "127.0.0.1:8888"}, ("RSS_AUDIO_MULLVAD_PROXY",)),
    ({"RSS_AUDIO_MAC_SSH": None}, ("RSS_AUDIO_MAC_SSH",)),
    ({"RSS_AUDIO_MAC_YTDLP_PATH": "yt-dlp"}, ("RSS_AUDIO_MAC_YTDLP_PATH",)),
    (dict.fromkeys(("RSS_AUDIO_MULLVAD_PROXY", "RSS_AUDIO_MAC_SSH", "RSS_AUDIO_MAC_YTDLP_PATH")),
     ("RSS_AUDIO_MULLVAD_PROXY", "RSS_AUDIO_MAC_SSH", "RSS_AUDIO_MAC_YTDLP_PATH")),
])
def test_validate_settings(change, named):
    if not named:
        validate_settings(settings())
        return
    with pytest.raises(RuntimeError) as err:
        validate_settings(settings(**change))
    assert all(name in str(err.value) for name in named)
