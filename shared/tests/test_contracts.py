"""Pure tables for the runtime contract every producer serializes through.

job_input_violations: job input -> violated fields
| input                                         | violations              |
| sourceText / https sourceUrl                  | -                       |
| both, None text, file:// url                  | the field(s)            |
| promptText / text anywhere                    | that key                |
| sourceText below the top level                | sourceText              |

runtime_contract: stable keys, metadata-only receipts, delivery defaults
intent_contract: typed intents serialize to the same shapes and refuse violations
"""
import pytest

from tg_tools_common.intent_contract import RuntimeJobIntent, TelegramSourceIdentity
from tg_tools_common.runtime_contract import (build_delivery_payload, delivery_key, job_input_violations,
                                              source_receipt_payload, submit_payload, telegram_channel_id,
                                              telegram_source_key)


@pytest.mark.parametrize("input_json, violations", [
    ({"sourceText": "read this"}, set()),
    ({"sourceUrl": "https://example.test/a", "voice": "nova"}, set()),
    ({"sourceText": "body", "sourceUrl": "https://example.test"}, {"sourceText", "sourceUrl"}),
    ({"sourceText": None}, {"sourceText"}),
    ({"sourceUrl": "file:///tmp/source"}, {"sourceUrl"}),
    ({"promptText": "no"}, {"promptText"}),
    ({"attachment": {"text": "no"}}, {"text"}),
    ({"nested": {"sourceText": "body"}}, {"sourceText"}),
])
def test_job_input_violations(input_json, violations):
    assert job_input_violations(input_json) == violations


def _receipt(source_json):
    return source_receipt_payload(source_key="tg:rss-audio:-100:55", producer_system="rss-audio",
                                  workload_type="rss_publish", route_id="rss.publish.telegram.v1",
                                  route_kind="rss_publish_route", channel_id="rss-audio:-100:22",
                                  matched_rule_version="v1", source_json=source_json)


def test_runtime_contract_keys_and_payloads():
    assert telegram_source_key("rss-audio", -100, 55) == "tg:rss-audio:-100:55"
    assert telegram_channel_id("rss-audio", -100, 22) == "rss-audio:-100:22"
    assert telegram_channel_id("rss-audio", -100, None) == "rss-audio:-100:0"
    assert delivery_key(owner_scope="tts", dedupe_key="final:1:2") == "tts:telegram-final:final:1:2"
    assert _receipt({"telegram": {"chatId": -100}, "attachmentCount": 1})["sourceJson"]["attachmentCount"] == 1

    job = submit_payload(producer_system="tts", producer_ref="source", result_owner="tts", result_route="route",
                         workload_type="tts_synthesize", priority=50, max_attempts=3, effect_key="tts:final:key",
                         input_json={"sourceText": "read this"})
    assert (job["inputJson"], job["idempotencyKey"], job["priority"], job["maxAttempts"], job["effectKey"]) == (
        {"sourceText": "read this"}, "source", 50, 3, "tts:final:key")

    delivery = build_delivery_payload(result_owner="tts", result_route="telegram.tts.confirmation.v1",
                                      delivery_key_str="k",
                                      payload_json={"chatId": 100, "replyToMessageId": 55, "text": "hello"})
    assert (delivery["deliveryProvider"], delivery["deliveryKind"], delivery["priority"], delivery["maxAttempts"]) == (
        "telegram", "telegram_reply_text", 70, 5)


@pytest.mark.parametrize("source_json", [
    {"promptText": "secret"}, {"nested": {"sourceText": "secret"}}, {"attachment": {"text": "secret"}},
    {"sourceUrl": "https://example.test/private"},
])
def test_runtime_contract_receipts_are_metadata_only(source_json):
    with pytest.raises(ValueError):
        _receipt(source_json)


def test_intent_contract():
    source = TelegramSourceIdentity(bot_identity="rss-audio", chat_id=-100, thread_id=22, message_id=55,
                                    reply_to_message_id=40)
    assert (source.source_key, source.channel_id) == ("tg:rss-audio:-100:55", "rss-audio:-100:22")
    assert source.metadata() == {"botIdentity": "rss-audio", "chatId": -100, "messageId": 55, "threadId": 22,
                                 "replyToMessageId": 40}

    def job(input_json):
        return RuntimeJobIntent(producer_system="p", producer_ref="source", result_owner="o", result_route="r",
                                workload_type="rss_publish", input_json=input_json).to_submit_payload()
    assert job({"sourceText": "read this"})["inputJson"] == {"sourceText": "read this"}
    with pytest.raises(ValueError, match="promptText"):
        job({"promptText": "no"})
