"""Failure policy and identity (pure tables).

classify_outcome(error_class)            -> (outcome, backoff s)
| input_empty / voice_invalid / audio_pace_implausible / every terminal source class | terminal, 0 |
| model_auth_required / publish_blocked / never_seen_before                           | retry 900 / 600 / 300 |
| extract_deferred (ocr_pending, recovery available)                                  | retry 120 |
| extract_deferred (any other reason, or no recovery)                                 | terminal (service_configuration_failure) |
publisher._classify_http: 403 1010 blocked, 400 UNIQUE duplicate, 409 conflict(terminal), 401 auth, 429/503 transient
validate_voice: provider-native voices only (Azure "nova", Gemini "kore"); "en-US-JennyNeural" on Gemini fails
execution_fingerprint: deterministic, voice case-insensitive, length-insensitive; mode/voice/model/roles/source change it
"""
import pytest

from tts_common import taxonomy
from tts_worker.failure_policy import classify_outcome, effective_error_class, validate_voice
from tts_worker.fingerprint import execution_fingerprint
from tts_worker.publisher import _classify_http


@pytest.mark.parametrize("error_class, kwargs, expected", [
    *[(cls, {}, ("terminal_error", 0)) for cls in
      ("input_empty", "voice_invalid", "audio_pace_implausible", *sorted(taxonomy.TERMINAL_SOURCE_CLASSES))],
    ("model_auth_required", {}, ("retryable_error", 900)),
    ("publish_blocked", {}, ("retryable_error", 600)),
    ("never_seen_before", {}, ("retryable_error", 300)),
    ("extract_deferred", {"reason_code": "ocr_pending", "recovery_available": True}, ("retryable_error", 120)),
    ("extract_deferred", {"reason_code": "ocr_pending"}, ("terminal_error", 0)),
    ("extract_deferred", {"reason_code": "queue_disabled", "recovery_available": True}, ("terminal_error", 0)),
])
def test_classify_outcome(error_class, kwargs, expected):
    assert classify_outcome(error_class, **kwargs) == expected


def test_every_taxonomy_class_has_an_outcome():
    assert classify_outcome(taxonomy.SOURCE_FETCH_TRANSIENT)[0] == "retryable_error"
    assert all(classify_outcome(cls)[0] in {"terminal_error", "retryable_error"} for cls in taxonomy.ALL_CLASSES)
    assert effective_error_class("extract_deferred", reason_code="unknown") == "service_configuration_failure"


@pytest.mark.parametrize("code, detail, error_class", [
    (403, "error 1010 browser_signature_banned", "publish_blocked"),
    (400, "D1_ERROR: UNIQUE constraint failed", "publish_duplicate_slug"),
    (409, "conflict", "publish_identity_conflict"),
    (401, "unauthorized", "publish_auth_required"),
    (429, "slow down", "publish_transient"),
    (503, "oops", "publish_transient"),
])
def test_classify_publish_http(code, detail, error_class):
    assert _classify_http(code, detail) == error_class


@pytest.mark.parametrize("provider, voice, ok", [("azure", "nova", True), ("gemini", "kore", True),
                                                 ("unknown-provider", "whatever", True),
                                                 ("gemini", "en-US-JennyNeural", False), ("azure", "kore-nope", False)])
def test_validate_voice(provider, voice, ok):
    if ok:
        validate_voice(provider, voice)
    else:
        with pytest.raises(ValueError):
            validate_voice(provider, voice)


def fp(**change):
    return execution_fingerprint(**{**dict(source_hash="a" * 64, mode="explain", requested_length="standard",
                                           voice="nova", speech_provider="azure", speech_model="gpt-4o-mini-tts",
                                           reader_roles={"write": "claude/claude-opus-5-5"}), **change})


def test_fingerprint():
    assert fp() == fp() == fp(voice="Nova") == fp(requested_length="long")
    changes = [dict(voice="ash"), dict(mode="read"), dict(speech_model="other"), dict(reader_roles={}),
               dict(source_hash="b" * 64)]
    assert len({fp(), *(fp(**c) for c in changes)}) == len(changes) + 1
