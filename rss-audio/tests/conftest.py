"""Every test starts with an enrichment tier file (the live ~/.config/tg-tools/enrichment.json is never read).

Tests of a missing or wrong file point TG_TOOLS_ENRICHMENT_FILE elsewhere themselves."""
import json

import pytest

TIERS = {"episodes": "haiku", "speech": "haiku", "surveillance": "sonnet"}


@pytest.fixture(autouse=True)
def enrichment_tiers(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("enrichment") / "enrichment.json"
    path.write_text(json.dumps(TIERS))
    monkeypatch.setenv("TG_TOOLS_ENRICHMENT_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def no_real_speech_to_text(monkeypatch):
    """No test reaches a real speech-to-text endpoint (m3-asr or Azure).

    The transcription tool never merges the host's secrets files (4 Nov: a fixture that no longer
    stubbed STT sent its mp3 to the live m3-asr), and an endpoint that is not loopback fails the test.
    Chains route STT to their local edge; unit tests stub pipeline._transcribe or pass module=.
    """
    import ipaddress
    from urllib.parse import urlsplit

    import episode_enrichment.stt as stt
    real_load, tried = stt.load_transcribe_module, []

    def loopback(url):
        host = urlsplit(url or "").hostname or ""
        try:
            return host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def load():
        module = real_load()
        monkeypatch.setattr(module, "_load_local_env", lambda: None)
        for name in ("load_self_hosted_config", "load_azure_openai_config"):
            real = getattr(module, name)

            def checked(*args, _real=real, **kwargs):
                config = _real(*args, **kwargs)
                if config is not None and not loopback(config.endpoint if hasattr(config, "endpoint") else config.url):
                    tried.append(name)
                    raise stt.TranscriptionError("tests do not run real speech-to-text", stage="config")
                return config
            monkeypatch.setattr(module, name, checked)
        return module
    monkeypatch.setattr(stt, "load_transcribe_module", load)
    yield
    assert not tried, f"a test reached real speech-to-text ({tried}); stub pipeline._transcribe or pass module="
