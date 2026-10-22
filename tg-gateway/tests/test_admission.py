"""Pure table for tg_gateway.admission: matched update -> source receipt + job submit payload.

Every admitted case also proves the receipt is metadata-only: the message text
never appears in it, and the job input carries no stray text field.

| route            | message                         | admission input            | job input (subset) / error                         |
| meeting.note     | caption + audio                 | attachment                 | no sourceText/sourceUrl; workload meeting_summary  |
| rss.publish      | url                             | text                       | sourceUrl                                          |
| rss.publish      | two urls                        | text                       | ValueError: exactly one URL                        |
| rss.publish      | url caption + audio file        | attachment                 | file source; caption url dropped                   |
| transcribe.audio | reply text                      | scribe telegram_text       | sourceText                                         |
| transcribe.audio | url + stray attachment          | scribe direct_audio_url    | sourceUrl, no telegramFile*                        |
| tts.synthesize   | "Read this aloud <url>"         | text                       | sourceUrl only, prose dropped                      |
| tts.synthesize   | plain text                      | text                       | sourceText                                         |
| tts.synthesize   | staged text document            | text_document ref          | telegram_document + localSourcePath + file id/name |
| x-tts.explain    | pdf document                    | document ref               | reader-v4, maxAttempts 2, six required artifacts   |
| x-tts.explain    | pdf + any tts selector          | tts_options                | ValueError: does not accept selectors              |
"""
import pytest

from tg_gateway.admission import AdmissionInput, build_route_admission
from tg_tools_common.telegram_manifest import build_registry_from_manifests, load_named_service_manifest
from tg_tools_common.telegram_routes import TelegramUpdateShape, normalize_command

CHAT = -100
THREADS = {"rss": 2, "transcribe": 3, "tts": 4, "x-tts": 5, "meeting": 6}
REGISTRY = build_registry_from_manifests(
    [load_named_service_manifest(name) for name in ("rss-audio", "tts", "meeting-summary")],
    bindings={"rss_chat_id": CHAT, "rss_publish_thread_id": 2, "rss_transcribe_thread_id": 3,
              "tts_chat_id": CHAT, "tts_thread_id": 4, "x_tts_thread_id": 5, "meeting_chat_id": CHAT,
              "meeting_thread_id": 6})
AUDIO = {"fileId": "file-1", "fileName": "audio.mp3"}
PDF = {"kind": "document", "fileId": "f1", "fileName": "source.pdf", "mimeType": "application/pdf"}
X_TTS_ARTIFACTS = ["x_tts_source_markdown", "x_tts_script_markdown", "x_tts_final_speech_text", "x_tts_audio_mp3",
                   "x_tts_primary_enriched_markdown", "x_tts_episode_manifest"]


def admit(lane, text="", *, media=(), reply=False, **admission):
    bot = "meeting-summary" if lane == "meeting" else "rss-audio"
    shape = TelegramUpdateShape(bot_identity=bot, chat_id=CHAT, thread_id=THREADS[lane], message_id=77,
                                sender_user_id=123, text=text, command=normalize_command(text),
                                media_kinds=frozenset(media), is_reply=reply, reply_to_message_id=70 if reply else None)
    admission.setdefault("text", text)
    return build_route_admission(shape=shape, resolution=REGISTRY.resolve(shape),
                                 admission_input=AdmissionInput(**admission))


@pytest.mark.parametrize("lane, text, media, extra, workload, expected, absent", [
    ("meeting", "meeting caption", ("audio",), {"attachment_refs": (AUDIO,)}, "meeting_summary", {},
     {"sourceText", "sourceUrl"}),
    ("rss", "https://example.com/feed", (), {}, "rss_publish", {"sourceUrl": "https://example.com/feed"}, set()),
    ("rss", "caption https://example.com/ignored", ("audio",), {"attachment_refs": (AUDIO,)}, "rss_publish", {},
     {"sourceText", "sourceUrl"}),
    ("transcribe", "private body", (), {"reply": True, "scribe": {"inputKind": "telegram_text"}}, "transcribe_audio",
     {"sourceText": "private body"}, set()),
    ("transcribe", "https://example.com/audio.mp3", (), {"attachment_refs": (AUDIO,),
     "scribe": {"inputKind": "direct_audio_url"}}, "transcribe_audio", {"sourceUrl": "https://example.com/audio.mp3"},
     {"telegramFileId", "telegramFileName"}),
    ("tts", "Read this aloud https://example.com/article", (), {}, "tts_synthesize",
     {"sourceUrl": "https://example.com/article"}, {"sourceText"}),
    ("tts", "private body", (), {}, "tts_synthesize", {"sourceText": "private body"}, set()),
    ("tts", "", ("document",), {"attachment_refs": ({"kind": "text_document", "path": "/tmp/source.md",
     "displayName": "source.md", "sourceFileId": "telegram-file-id"},)}, "tts_synthesize",
     {"inputKind": "telegram_document", "localSourcePath": "/tmp/source.md", "telegramFileName": "source.md",
      "telegramFileId": "telegram-file-id"}, {"sourceText", "sourceUrl"}),
    ("x-tts", "", ("document",), {"attachment_refs": (PDF,)}, "x_tts_explain", {"promptVersion": "reader-v4"},
     {"length", "model", "backend", "promptSelector"}),
])
def test_admitted(lane, text, media, extra, workload, expected, absent):
    admission = admit(lane, text, media=media, **extra)
    receipt, job = admission.source_receipt_payload, admission.job_submit_payload
    assert job["workloadType"] == workload
    assert {k: job["inputJson"][k] for k in expected} == expected
    assert not (absent | {"text"}) & job["inputJson"].keys()
    assert receipt["sourceKey"] == job["inputJson"].get("sourceKey", receipt["sourceKey"])
    assert not text or text not in repr(receipt)
    if workload == "x_tts_explain":
        assert job["maxAttempts"] == 2 and job["outputContractJson"]["requiredArtifacts"] == X_TTS_ARTIFACTS


@pytest.mark.parametrize("lane, text, media, extra, error", [
    ("rss", "https://a.example/1 https://b.example/2", (), {}, "exactly one URL"),
    *[("x-tts", "", ("document",), {"attachment_refs": (PDF,), "tts_options": {selector: "any"}},
       "does not accept selectors") for selector in ("length", "model", "backend", "promptSelector")],
])
def test_rejected(lane, text, media, extra, error):
    with pytest.raises(ValueError, match=error):
        admit(lane, text, media=media, **extra)
