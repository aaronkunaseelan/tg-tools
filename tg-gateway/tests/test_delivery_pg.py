"""Real PostgreSQL + HTTP edge for gateway delivery: DeliveryDrain over
PostgresGatewayState (migrated schema) and BotApiSender -> local Bot API.

The contract: one provider effect per delivery, durable across process loss.
| case                                   | edge                         | outcome                                        |
| plain send, then redrain               | 200 ok                       | sent, then already_sent; one request           |
| applied, response unreadable, redrain  | 200 non-JSON                 | uncertain, then uncertain; never resent        |
| explicit 429 refusal, then retry       | 429 retry_after 5, then ok   | retryable(5), flood_state row, then sent       |
| explicit 400 refusal                   | 400                          | dead, intent released                          |
| two dispatchers race one delivery      | -                            | exactly one begin_dispatch wins                |
| key shared by a second delivery        | receipt for the first id     | first replays its id; second sends once        |
| same Telegram update claimed twice     | -                            | claimed once                                   |
"""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from chain_harness import start_stack
from tg_gateway.bot_api import BotApiClient
from tg_gateway.daemon import BotApiSender
from tg_gateway.delivery import DeliveryDrain, DeliveryRow
from tg_gateway.pg_state import PostgresGatewayState, PostgresHandledUpdateStore


@pytest.fixture(scope="module")
def stack():
    stack = start_stack(controller=False)
    yield stack
    stack.stop()


def drain(stack):
    sender = BotApiSender(BotApiClient(token="1:t", base_url=stack.edge.url))
    return DeliveryDrain(sender=sender, state=PostgresGatewayState(dsn=stack.dsn()))


def row(key=None, chat=-100):
    return DeliveryRow(str(uuid4()), key or str(uuid4()), "telegram_text", {"chatId": chat, "text": "final"})


def calls(stack, chat):
    return [r for r in stack.edge.requests if r.path.endswith("/sendMessage") and r.json()["chat_id"] == chat]


def test_sent_once_then_replayed(stack):
    delivery = row(chat=-1)
    first = drain(stack).drain_row(delivery)
    assert first.status == "sent"
    assert drain(stack).drain_row(delivery) == type(first)("already_sent", first.telegram_message_id)
    assert len(calls(stack, -1)) == 1


def test_unreadable_success_is_never_resent(stack):
    stack.edge.route("POST", r"/sendMessage$", lambda req: (200, b"<html>proxy</html>")
                     if req.json()["chat_id"] == -2 else stack.edge._telegram(req))
    delivery = row(chat=-2)
    assert [drain(stack).drain_row(delivery).status for _ in range(2)] == ["uncertain", "uncertain"]
    assert len(calls(stack, -2)) == 1


@pytest.mark.parametrize("chat, status, retry_after, outcome", [(-3, 429, 5, "retryable"), (-4, 400, None, "dead")])
def test_explicit_refusal_releases_intent(stack, chat, status, retry_after, outcome):
    refused = []

    def refuse_once(req):
        if req.json()["chat_id"] != chat or refused:
            return stack.edge._telegram(req)
        refused.append(req)
        return status, {"ok": False, "error_code": status, "description": "refused",
                        "parameters": {"retry_after": retry_after}}
    stack.edge.route("POST", r"/sendMessage$", refuse_once)
    delivery = row(chat=chat)
    first = drain(stack).drain_row(delivery)
    assert (first.status, first.retry_after_seconds) == (outcome, retry_after)
    assert stack.psql(f"SELECT count(*) FROM tg_gateway.flood_state WHERE chat_id = {chat}") == str(int(bool(retry_after)))
    assert drain(stack).drain_row(delivery).status == "sent"
    assert len(calls(stack, chat)) == 2


def test_racing_dispatchers_have_one_effect_owner(stack):
    delivery = row()
    states = [PostgresGatewayState(dsn=stack.dsn()) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        won = list(pool.map(lambda s: s.begin_dispatch(delivery_id=delivery.delivery_id,
                                                       delivery_key=delivery.delivery_key,
                                                       dispatch_token=str(uuid4())), states))
    assert sorted(won) == [False, True]


def test_shared_key_receipt_is_per_delivery(stack):
    old, new = row("shared-key", chat=-5), row("shared-key", chat=-6)
    PostgresGatewayState(dsn=stack.dsn()).remember_sent(delivery_id=old.delivery_id, delivery_key=old.delivery_key,
                                                         chat_id=-5, topic_id=None, telegram_message_id=1234)
    assert drain(stack).drain_row(old).telegram_message_id == 1234
    assert drain(stack).drain_row(new).status == "sent"
    assert drain(stack).drain_row(old).telegram_message_id == 1234
    assert (len(calls(stack, -5)), len(calls(stack, -6))) == (0, 1)


def test_update_claimed_once(stack):
    store = PostgresHandledUpdateStore(dsn=stack.dsn())
    assert [store.claim_update("message:-100:9"), store.claim_update("message:-100:9")] == [True, False]
    assert store.disposition("message:-100:9") == "uncertain"
    store.mark_handled("message:-100:9")
    assert store.disposition("message:-100:9") == "handled"
