"""Pure table for tg_tools_common.telegram_manifest: manifest + bindings -> registry or a local error.

| case                  | change to a valid manifest      | outcome                                   |
| valid                 | -                               | one route, resolves a url in its topic    |
| optional binding None | thread binding None             | route skipped (no routes)                 |
| missing binding       | thread binding absent           | ValueError "missing binding ..."          |
| duplicate route id    | route repeated                  | ValueError "duplicate route_id"           |
| invalid route kind    | routeKind "invalid_route"       | ValueError "invalid routeKind"            |
| invalid text mode     | textModes ["invalid_text"]      | ValueError "invalid value"                |
| missing field         | no chatIdBinding                | ValueError naming chatIdBinding           |
| shipped manifests     | services/*.json                 | every declared route id loads             |
"""
import copy

import pytest

from tg_tools_common.telegram_manifest import (build_registry_from_manifests, load_named_service_manifest,
                                               parse_service_manifest)
from tg_tools_common.telegram_routes import TelegramUpdateShape

VALID = {"service": "utility", "botIdentity": "utility-bot", "ownerSystem": "utility", "routes": [{
    "routeId": "utility.url.telegram.v1", "routeKind": "rss_publish_route", "workloadType": "rss_publish",
    "chatIdBinding": "utility_chat_id", "threadMode": "exact", "threadIdBinding": "utility_thread_id",
    "rules": [{"ruleId": "url", "textModes": ["url"], "mediaKinds": ["audio"], "requireReply": False}]}]}
BINDINGS = {"utility_chat_id": -100, "utility_thread_id": 20}


def test_valid_manifest_resolves():
    registry = build_registry_from_manifests([parse_service_manifest(VALID)], bindings=BINDINGS)
    resolved = registry.resolve(TelegramUpdateShape(bot_identity="utility-bot", chat_id=-100, thread_id=20,
                                                    message_id=5, text="https://example.com/file.mp3"))
    assert resolved.route.route_id == "utility.url.telegram.v1"
    none_binding = build_registry_from_manifests([parse_service_manifest(VALID)],
                                                 bindings={**BINDINGS, "utility_thread_id": None})
    assert none_binding.routes == ()


def _edit(path, value):
    raw = copy.deepcopy(VALID)
    *parents, key = path
    target = raw
    for part in parents:
        target = target[part]
    if value is None:
        del target[key]
    else:
        target[key] = value
    return raw


@pytest.mark.parametrize("raw, bindings, error", [
    (VALID, {"utility_chat_id": -100}, "missing binding utility_thread_id"),
    (_edit(["routes"], VALID["routes"] * 2), BINDINGS, "duplicate route_id"),
    (_edit(["routes", 0, "routeKind"], "invalid_route"), BINDINGS, "invalid routeKind"),
    (_edit(["routes", 0, "rules", 0, "textModes"], ["invalid_text"]), BINDINGS, "invalid value"),
    (_edit(["routes", 0, "chatIdBinding"], None), BINDINGS, "chatIdBinding"),
])
def test_invalid_manifest_or_bindings_fail_locally(raw, bindings, error):
    with pytest.raises(ValueError, match=error):
        build_registry_from_manifests([parse_service_manifest(raw)], bindings=bindings)


def test_shipped_manifests_declare_the_five_routes():
    route_ids = {route.route_id for name in ("rss-audio", "tts", "meeting-summary")
                 for route in load_named_service_manifest(name).routes}
    assert route_ids == {"rss.publish.telegram.v1", "transcribe.audio.telegram.v1",
                         "tts.synthesize.telegram.v1", "x-tts.explain.telegram.v1", "meeting.note.telegram.v1"}
