"""Exercise real gateway admission, handoff, downloader and dispatch ordering."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from tg_gateway.admission import AdmissionInput
from tg_gateway.daemon import handle_message
from tg_gateway.models import InboundAttachment, InboundMessage
from tg_gateway.source_handoff import SourceHandoffDrainer
from tg_gateway.telethon_gateway import TelethonGateway
from tg_tools_common.telegram_manifest import build_registry_from_manifests, load_named_service_manifest


@pytest.fixture(autouse=True)
def deterministic_executor(monkeypatch):
    # This sandbox cannot wake asyncio through its cross-thread socket. These
    # regressions concern ordering and dispositions, so run the same blocking
    # calls inline; every persistence, admission and staging call still runs.
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', inline)


class DurableHandoff:
    def __init__(self, path):
        self.path = path
        self.row = None

    def save(self):
        self.path.write_text(json.dumps(self.row))

    def create_handoff(self, row):
        if self.row is not None:
            return False
        self.row = dict(row, handoff_id=1, lease_token='synthetic-lease', state='pending')
        self.save()
        return True

    def claim_next(self, **kwargs):
        return self.row

    def record_staged(self, handoff_id, **kwargs):
        self.row.update(state='staged', staged_path=kwargs['path'],
                        staged_sha256=kwargs['sha256'], intent_json=kwargs['intent_json'])
        self.save()

    def record_receipt(self, handoff_id, receipt_id, **kwargs):
        self.row['receipt_id'] = receipt_id
        self.save()

    def record_job(self, handoff_id, job_id, **kwargs):
        self.row['job_id'] = job_id
        self.save()

    def mark_submitted(self, handoff_id, **kwargs):
        self.row['state'] = 'submitted'
        self.save()

    def mark_terminal_rejected(self, handoff_id, error, **kwargs):
        self.row.update(state='terminal_rejected', last_error=error)
        self.save()


class Runtime:
    def __init__(self):
        self.deliveries = []

    def admit_source_receipt(self, payload):
        return {'receipt': {'receiptId': 'receipt-1'}}

    def submit_job(self, payload):
        return {'job': {'jobId': 'job-1'}}

    def submit_source_receipt(self, **kwargs):
        return {}

    def append_delivery(self, payload):
        self.deliveries.append(payload)
        return {}


class Handled:
    def __init__(self):
        self.claimed = set()
        self.completed = set()

    def claim_update(self, uid):
        if uid in self.claimed:
            return False
        self.claimed.add(uid)
        return True


    def disposition(self, uid):
        return 'handled' if uid in self.completed else 'uncertain'

    def mark_handled(self, uid):
        self.completed.add(uid)


class Adapter:
    def __init__(self):
        self.executions = 0

    async def handle_message(self, *args, **kwargs):
        return False

    async def handle_command(self, *args, **kwargs):
        self.executions += 1
        if self.executions == 1:
            raise RuntimeError('synthetic failure before command effect')
        return True

    def prepare_admission(self, runtime, message, *, attachment_refs, **kwargs):
        return AdmissionInput(text=message.text or '', attachment_refs=attachment_refs)


def setup_runtime(tmp_path, *, command=False, missing_media=False):
    registry = build_registry_from_manifests(
        [load_named_service_manifest('meeting-summary')],
        bindings={'meeting_chat_id': -100, 'meeting_thread_id': 6})
    message = InboundMessage(
        receipt_key='source', chat_id=-100, thread_id=6, message_id=77, user_id=123,
        text='/status' if command else '',
        attachments=() if command else (InboundAttachment(
            kind='audio', file_id='mtmsg:-100:77', file_name='synthetic.mp3',
            mime_type='audio/mpeg', file_size=16, display_name='synthetic.mp3'),))
    store = DurableHandoff(tmp_path / 'handoff.json')
    api = Runtime()
    seen_at_ack = []
    staging = tmp_path / 'staging'

    async def reply(outbound):
        seen_at_ack.append(any(p.is_file() for p in staging.rglob('*')))
        return 100

    async def get_entity(chat_id):
        return chat_id

    async def get_messages(entity, ids):
        return None

    missing = TelethonGateway(client=NS(get_messages=get_messages))
    missing._get_entity = get_entity

    def downloader(file_id, target):
        if missing_media:
            # The real transport produces its actual unavailable-media error.
            return asyncio.run(missing.download_file(file_id, target_path=target)).path
        target.write_bytes(b'synthetic media!')
        return str(target)

    adapter = Adapter()
    runtime = NS(
        settings=NS(bot_identity='meeting-summary', bot_username='synthetic_bot',
                    allowed_chat_id=-100, allowed_thread_id=6,
                    allowed_user_ids=(123,), progress_workloads=()),
        gateway=NS(normalize_message=lambda event: message, send_text=reply),
        registry=registry, adapter=adapter, handled_updates=Handled(),
        source_handoffs=store, runtime_client=api, progress=None,
        handoff_drainer=SourceHandoffDrainer(
            store=store, runtime=api, staging_root=staging,
            downloader=downloader, bot_identity='meeting-summary'))
    return runtime, NS(message=object()), store, api, seen_at_ack


def test_media_is_retained_when_gateway_acknowledges(tmp_path):
    runtime, event, store, api, seen = setup_runtime(tmp_path)
    asyncio.run(handle_message(runtime, event))
    assert store.row['state'] == 'submitted'
    assert seen == [True], f'input retained at acknowledgement: {seen}'


def test_deleted_media_reaches_a_durable_rejection(tmp_path):
    runtime, event, store, api, seen = setup_runtime(tmp_path, missing_media=True)
    drainer = runtime.handoff_drainer
    # Persist intake through the real handler, then run the actual drainer
    # outside the event loop, just as the production executor would do.
    runtime.handoff_drainer = NS(drain_once=lambda **kwargs: 'deferred')
    asyncio.run(handle_message(runtime, event))
    error = None
    try:
        drainer.drain_once()
    except Exception as exc:
        error = type(exc).__name__
    state = json.loads(store.path.read_text())['state']
    assert state == 'terminal_rejected' and len(api.deliveries) == 1, (
        f'deleted media: state={state}, rejection deliveries={len(api.deliveries)}, error={error}')


def test_completed_command_is_not_repeated(tmp_path):
    runtime, event, store, api, seen = setup_runtime(tmp_path, command=True)
    runtime.adapter.executions = 1
    asyncio.run(handle_message(runtime, event))
    asyncio.run(handle_message(runtime, event))
    assert runtime.adapter.executions == 2
    assert not api.deliveries


def test_interrupted_command_is_reported_without_repeating_effects(tmp_path):
    runtime, event, store, api, seen = setup_runtime(tmp_path, command=True)
    runtime.handled_updates.claim_update('message:meeting-summary:-100:77')
    asyncio.run(handle_message(runtime, event))
    assert runtime.adapter.executions == 0
    assert len(api.deliveries) == 1
    assert 'uncertain' in api.deliveries[0]['payloadJson']['text']


def test_failed_command_has_recovery_or_a_visible_outcome(tmp_path):
    runtime, event, store, api, seen = setup_runtime(tmp_path, command=True)
    try:
        asyncio.run(handle_message(runtime, event))
    except RuntimeError:
        pass
    asyncio.run(handle_message(runtime, event))
    assert runtime.adapter.executions == 2 or api.deliveries or seen, (
        f'command after failure and replay: executions={runtime.adapter.executions}, '
        f'outcome deliveries={len(api.deliveries)}, replies={len(seen)}')

