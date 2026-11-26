"""Several links in one rss-audio message: one episode each, and refusals the owner can see.

The owner sent the bot three YouTube links in one message (space-separated, then
comma-separated). Before this, the rss admission refused the message with "RSS
sources accept exactly one URL per message" and the daemon turned that ValueError
into a journal traceback, so nothing was queued and the owner saw nothing.
"""
import asyncio
import contextlib
import json
from types import SimpleNamespace as NS

from tg_gateway.admission import MAX_LINKS, AdmissionInput, build_route_admission, extract_urls
from tg_gateway.daemon import handle_message
from tg_gateway.models import InboundMessage
from tg_gateway.source_handoff import SourceHandoffDrainer
from tg_tools_common.telegram_manifest import build_registry_from_manifests, load_named_service_manifest
from tg_tools_common.telegram_routes import TelegramUpdateShape, normalize_command

CHAT = -100
BINDINGS = {"rss_chat_id": CHAT, "rss_publish_thread_id": 2, "rss_transcribe_thread_id": 3}
REGISTRY = build_registry_from_manifests(
    [load_named_service_manifest("rss-audio")], bindings=BINDINGS)

# The owner's two messages: the same three links, spaces then commas.
THREE_URLS = ["https://www.youtube.com/watch?v=RinvlgAG_9Y",
              "https://www.youtube.com/watch?v=UwUllHcbLpg",
              "https://youtu.be/gLzmqr2Wk8Y"]
SPACED = " ".join(THREE_URLS)
COMMA = ", ".join(THREE_URLS)


def shape_for(text, *, thread=2, message_id=77):
    return TelegramUpdateShape(bot_identity="rss-audio", chat_id=CHAT, thread_id=thread,
                               message_id=message_id, sender_user_id=123, text=text,
                               command=normalize_command(text), media_kinds=frozenset())


def admit_rss(text, **admission):
    shape = shape_for(text)
    admission.setdefault("text", text)
    return build_route_admission(shape=shape, resolution=REGISTRY.resolve(shape),
                                 admission_input=AdmissionInput(**admission))


# -- extract_urls ------------------------------------------------------------

def test_extract_urls_splits_spaces_commas_and_newlines():
    assert extract_urls(SPACED) == THREE_URLS
    assert extract_urls(COMMA) == THREE_URLS
    assert extract_urls("\n".join(THREE_URLS)) == THREE_URLS
    assert extract_urls(f"{THREE_URLS[0]} {THREE_URLS[0]} {THREE_URLS[1]}") == THREE_URLS[:2]
    assert extract_urls("read https://example.com/a. and see (https://example.com/b)") == [
        "https://example.com/a", "https://example.com/b"]
    assert extract_urls("no links here") == []


# -- part keys ---------------------------------------------------------------

def test_rss_part_keys_are_distinct_and_part_none_is_unchanged():
    admitted = [admit_rss(THREE_URLS[i - 1], part=i) for i in (1, 2, 3)]
    source_keys = [a.source_receipt_payload["sourceKey"] for a in admitted]
    assert source_keys == [f"tg:rss-audio:{CHAT}:77:u{i}" for i in (1, 2, 3)]
    for part, admission in enumerate(admitted, start=1):
        job, receipt = admission.job_submit_payload, admission.source_receipt_payload
        key = f"tg:rss-audio:{CHAT}:77:u{part}"
        assert receipt["sourceKey"] == key
        assert job["producerRef"] == key
        assert job["idempotencyKey"] == key
        assert job["effectKey"] == f"rss-audio:feed-publish:{key}"
        assert job["inputJson"]["sourceKey"] == key
        assert job["inputJson"]["sourceUrl"] == THREE_URLS[part - 1]
        # The message is still one message: replies go to it, not to a part.
        assert job["inputJson"]["messageRef"] == {"chat_id": CHAT, "message_thread_id": 2,
                                                  "message_id": 77, "reply_to_message_id": None}
        assert job["inputJson"]["status"] == {"chatId": CHAT, "threadId": 2, "messageId": None}

    # part=None is today's payload, byte for byte.
    admission = admit_rss("https://example.com/feed")
    assert admission.source_receipt_payload == {
        "sourceSystem": "telegram",
        "sourceKey": "tg:rss-audio:-100:77",
        "producerSystem": "rss-audio",
        "workloadType": "rss_publish",
        "routeId": "rss.publish.telegram.v1",
        "routeKind": "rss_publish_route",
        "channelId": "rss-audio:-100:2",
        "matchedRuleVersion": "v1",
        "sourceJson": {
            "telegram": {"botIdentity": "rss-audio", "chatId": -100, "messageId": 77, "threadId": 2},
            "schemaVersion": 1, "botIdentity": "rss-audio", "chatId": -100, "threadId": 2,
            "messageId": 77, "senderUserId": 123, "inputKind": "url", "jobKind": "rss"},
    }
    assert admission.job_submit_payload == {
        "producerSystem": "rss-audio",
        "producerRef": "tg:rss-audio:-100:77",
        "resultOwner": "rss-audio",
        "resultRoute": "telegram.rss-audio.confirmation.v1",
        "workloadType": "rss_publish",
        "queueClass": "foreground",
        "executionProfile": "cpu_media",
        "executionShape": "local_bounded",
        "priority": 60,
        "inputItemCount": 1,
        "inputJson": {
            "kind": "rss",
            "sourceKey": "tg:rss-audio:-100:77",
            "messageRef": {"chat_id": -100, "message_thread_id": 2, "message_id": 77,
                           "reply_to_message_id": None},
            "inputKind": "url",
            "status": {"chatId": -100, "threadId": 2, "messageId": None},
            "sourceUrl": "https://example.com/feed",
        },
        "idempotencyKey": "tg:rss-audio:-100:77",
        "maxAttempts": 3,
        "effectKey": "rss-audio:feed-publish:tg:rss-audio:-100:77",
        "outputContractJson": {"version": 1,
                               "result": "rss episode publication plus telegram confirmation delivery"},
    }


# -- the gateway path --------------------------------------------------------

@contextlib.contextmanager
def inline_threads():
    """Run the daemon's blocking calls inline, as a deterministic executor would."""
    original = asyncio.to_thread

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    asyncio.to_thread = inline
    try:
        yield
    finally:
        asyncio.to_thread = original


class Store:
    """Durable handoff store that keeps every row, keyed by update_uid."""

    def __init__(self):
        self.rows = {}
        self.rejected = []

    def create_handoff(self, row):
        if row["update_uid"] in self.rows:
            return False
        handoff_id = len(self.rows) + 1
        self.rows[row["update_uid"]] = dict(row, handoff_id=handoff_id,
                                           lease_token=f"lease-{handoff_id}", state="pending")
        return True

    def claim_next(self, *, owner, bot_identity, lease_seconds, update_uid=None):
        row = self.rows.get(update_uid) if update_uid else None
        return row if row is not None and row["state"] == "pending" else None

    def record_staged(self, handoff_id, **kwargs):
        raise AssertionError("no attachment is staged on these routes")

    def record_receipt(self, handoff_id, receipt_id, **kwargs):
        self.row_by_id(handoff_id)["receipt_id"] = receipt_id

    def record_job(self, handoff_id, job_id, **kwargs):
        self.row_by_id(handoff_id)["job_id"] = job_id

    def mark_submitted(self, handoff_id, **kwargs):
        self.row_by_id(handoff_id)["state"] = "submitted"

    def mark_terminal_rejected(self, handoff_id, error, **kwargs):
        row = self.row_by_id(handoff_id)
        row.update(state="terminal_rejected", last_error=error)
        self.rejected.append(row)

    def row_by_id(self, handoff_id):
        return next(row for row in self.rows.values() if row["handoff_id"] == handoff_id)


class Runtime:
    def __init__(self):
        self.submitted = []
        self.deliveries = []

    def admit_source_receipt(self, payload):
        return {"receipt": {"receiptId": f"receipt-{len(self.submitted) + 1}"}}

    def submit_job(self, payload):
        self.submitted.append(payload)
        return {"job": {"jobId": f"job-{len(self.submitted)}"}}

    def submit_source_receipt(self, **kwargs):
        return {}

    def append_delivery(self, payload):
        self.deliveries.append(payload)
        return {}


class Adapter:
    """The rss identity's admission, without product logic."""

    def __init__(self, **admission):
        self.admission = admission

    async def handle_message(self, *args, **kwargs):
        return False

    def prepare_admission(self, runtime, message, *, attachment_refs, **kwargs):
        return AdmissionInput(text=(message.text or "").strip(),
                              attachment_refs=attachment_refs, **self.admission)


def setup_runtime(*, thread=2, adapter=None):
    message = InboundMessage(receipt_key="source", chat_id=CHAT, thread_id=thread, message_id=77,
                             user_id=123, text="", attachments=())
    store = Store()
    api = Runtime()
    replies = []

    async def send_text(outbound):
        replies.append(outbound.text)
        return 100

    adapter = adapter or Adapter()
    runtime = NS(
        settings=NS(bot_identity="rss-audio", bot_username="synthetic_bot", allowed_chat_id=CHAT,
                    allowed_thread_id=thread, allowed_user_ids=(123,), progress_workloads=()),
        gateway=NS(normalize_message=lambda event: runtime.message, send_text=send_text),
        registry=REGISTRY, adapter=adapter, handled_updates=NS(claim_update=lambda uid: True),
        source_handoffs=store, runtime_client=api, progress=None,
        handoff_drainer=SourceHandoffDrainer(store=store, runtime=api,
                                             staging_root=NS(), downloader=lambda *a: "",
                                             bot_identity="rss-audio"))
    runtime.message = message
    return runtime, store, api, replies


def deliver(runtime, *, text):
    runtime.message = InboundMessage(receipt_key="source", chat_id=CHAT,
                                     thread_id=runtime.message.thread_id, message_id=77,
                                     user_id=123, text=text, attachments=())
    with inline_threads():
        asyncio.run(handle_message(runtime, NS(message=object())))


def test_a_message_with_three_links_hands_off_three_jobs():
    runtime, store, api, replies = setup_runtime()
    deliver(runtime, text=COMMA)
    uids = [f"message:rss-audio:{CHAT}:77:u{i}" for i in (1, 2, 3)]
    assert sorted(store.rows) == uids
    assert [store.rows[uid]["source_key"] for uid in uids] == [
        f"tg:rss-audio:{CHAT}:77:u{i}" for i in (1, 2, 3)]
    assert {store.rows[uid]["state"] for uid in uids} == {"submitted"}
    assert [api.submitted[i]["inputJson"]["sourceUrl"] for i in range(3)] == THREE_URLS
    assert {job["effectKey"] for job in api.submitted} == {
        f"rss-audio:feed-publish:tg:rss-audio:{CHAT}:77:u{i}" for i in (1, 2, 3)}
    assert replies == ["Received — queued 3 links."]

    # A redelivered update creates nothing new and says nothing.
    deliver(runtime, text=COMMA)
    assert sorted(store.rows) == uids
    assert len(api.submitted) == 3
    assert replies == ["Received — queued 3 links."]


def test_eleven_links_are_refused_with_a_reply():
    runtime, store, api, replies = setup_runtime()
    deliver(runtime, text=" ".join(f"https://example.com/{i}" for i in range(MAX_LINKS + 1)))
    assert store.rows == {}
    assert api.submitted == []
    assert replies == [
        "At most 10 links per message; nothing was queued. Send them in smaller messages."]


def test_an_admission_refusal_is_replied_not_raised():
    runtime, store, api, replies = setup_runtime(thread=3)
    deliver(runtime, text="https://a.example/1 https://b.example/2")
    assert replies == ["Transcription sources accept exactly one URL per message"]
    assert store.rows == {}
