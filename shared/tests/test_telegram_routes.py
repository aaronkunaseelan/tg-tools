"""Pure table for tg_tools_common.telegram_routes over the three declared manifests.

| bot             | thread | update                       | status    | route / rule                                          |
| rss-audio       | 10     | url                          | matched   | rss.publish / rss-url-or-audio                        |
| rss-audio       | 10     | plain text                   | unmatched | -                                                     |
| rss-audio       | 20     | audio                        | matched   | transcribe.audio / scribe-start-url-or-audio          |
| rss-audio       | 20     | text reply                   | matched   | transcribe.audio / scribe-append-reply-text-or-audio  |
| rss-audio       | 20     | text, not a reply            | unmatched | -                                                     |
| rss-audio       | 30     | text / document              | matched   | tts.synthesize / tts-text-url-or-file                 |
| rss-audio       | 40     | url                          | matched   | x-tts.explain / x-tts-text-url-or-file                |
| meeting-summary | 50     | document audio               | matched   | meeting.note / meeting-audio                          |
| meeting-summary | 50     | text                         | unmatched | - (but scoped to meeting.note: not another bot's)     |
| meeting-summary | 10     | audio in rss-audio's topic   | unmatched | - (scoped to nothing: another bot's topic)            |
| two routes      | same   | url matching both            | ambiguous | both candidate ids                                    |
"""
import pytest

from tg_tools_common.telegram_manifest import build_registry_from_manifests, load_named_service_manifest
from tg_tools_common.telegram_routes import (RouteDefinition, RouteRule, TelegramRouteRegistry, TelegramUpdateShape,
                                             normalize_command, telegram_source_key)

CHAT = -1001
REGISTRY = build_registry_from_manifests(
    [load_named_service_manifest(name) for name in ("rss-audio", "tts", "meeting-summary")],
    bindings={"rss_chat_id": CHAT, "rss_publish_thread_id": 10, "rss_transcribe_thread_id": 20,
              "tts_chat_id": CHAT, "tts_thread_id": 30, "x_tts_thread_id": 40, "meeting_chat_id": CHAT,
              "meeting_thread_id": 50})


def shape(bot, thread, *, text="", media=(), reply=False):
    return TelegramUpdateShape(bot_identity=bot, chat_id=CHAT, thread_id=thread, message_id=7, text=text,
                               command=normalize_command(text), media_kinds=frozenset(media), is_reply=reply,
                               reply_to_message_id=6 if reply else None)


@pytest.mark.parametrize("update, route, rule", [
    (shape("rss-audio", 10, text="see https://example.com/a.mp3"), "rss.publish.telegram.v1", "rss-url-or-audio"),
    (shape("rss-audio", 10, text="no link"), None, None),
    (shape("rss-audio", 20, media=["audio"]), "transcribe.audio.telegram.v1", "scribe-start-url-or-audio"),
    (shape("rss-audio", 20, text="add this", reply=True), "transcribe.audio.telegram.v1",
     "scribe-append-reply-text-or-audio"),
    (shape("rss-audio", 20, text="add this"), None, None),
    (shape("rss-audio", 30, text="read this aloud"), "tts.synthesize.telegram.v1", "tts-text-url-or-file"),
    (shape("rss-audio", 30, media=["document"]), "tts.synthesize.telegram.v1", "tts-text-url-or-file"),
    (shape("rss-audio", 40, text="https://doi.org/10.1/x"), "x-tts.explain.telegram.v1", "x-tts-text-url-or-file"),
    (shape("meeting-summary", 50, media=["document_audio"]), "meeting.note.telegram.v1", "meeting-audio"),
    (shape("meeting-summary", 50, text="not audio"), None, None),
    (shape("meeting-summary", 10, media=["audio"]), None, None),
])
def test_resolve(update, route, rule):
    resolved = REGISTRY.resolve(update)
    assert resolved.status == ("matched" if route else "unmatched")
    assert (resolved.route.route_id, resolved.route.matched_rule_id) == (route, rule) if route else resolved.route is None


@pytest.mark.parametrize("update, scoped", [
    (shape("meeting-summary", 50, text="not audio"), ("meeting.note.telegram.v1",)),
    (shape("meeting-summary", 10, text="not audio"), ()),
])
def test_scoped_route_ids_tell_own_unsupported_from_another_bots_topic(update, scoped):
    assert REGISTRY.scoped_route_ids(update) == scoped


def test_overlapping_routes_are_ambiguous():
    def route(route_id):
        return RouteDefinition(route_id=route_id, route_kind="rss_publish_route", workload_type="rss_publish",
                               owner_system="rss-audio", bot_identity="b", chat_id=CHAT, thread_id=5,
                               rules=(RouteRule(rule_id="url", text_modes=frozenset({"url"})),))
    resolved = TelegramRouteRegistry([route("a"), route("b")]).resolve(shape("b", 5, text="https://example.com"))
    assert (resolved.status, resolved.candidate_route_ids) == ("ambiguous", ("a", "b"))


def test_source_key_and_command_normalization():
    assert telegram_source_key(shape("rss-audio", 1)) == f"tg:rss-audio:{CHAT}:7"
    assert [normalize_command(t) for t in ("/Mode@utility_bot explain", "/", "hello")] == ["mode", None, None]
