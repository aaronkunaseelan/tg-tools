"""Model the controller's oldest-unconsumed page; use the real TTS projector."""
import pytest

from tts_worker.result_projector import ResultProjector


@pytest.fixture(autouse=True)
def configured_feed(monkeypatch):
    monkeypatch.setenv('TTS_FEED_TITLE', 'Synthetic feed')


class ResultView:
    def __init__(self, kinds):
        self.rows = [dict(resultId=f'result-{i}', jobId=f'job-{i}',
                          resultKind=kind, resultJson={'ok': kind == 'succeeded'})
                     for i, kind in enumerate(kinds)]
        self.consumed = set()
        self.deliveries = []

    def list_results(self, *, result_owner, limit):
        return [row for row in self.rows if row['resultId'] not in self.consumed][:limit]

    def get_job(self, *, job_id):
        return dict(jobId=job_id, resultOwner='tts',
                    resultRoute='telegram.tts.confirmation.v1',
                    inputJson={'sourceKey': job_id,
                               'messageRef': {'chat_id': -100, 'message_id': 77}})

    def append_delivery(self, payload):
        self.deliveries.append(payload)

    def consume_result(self, *, result_id, result_owner):
        self.consumed.add(result_id)


def test_success_without_cancellations_is_delivered():
    client = ResultView(['succeeded'])
    assert ResultProjector(client, owners=('tts',)).project_once() == 1
    assert client.consumed == {'result-0'} and len(client.deliveries) == 1


def test_single_cancellation_does_not_hide_success_in_same_page():
    client = ResultView(['cancelled', 'succeeded'])
    ResultProjector(client, owners=('tts',)).project_once()
    assert client.consumed == {'result-0', 'result-1'} and len(client.deliveries) == 2


def test_cancelled_is_projected_and_consumed():
    client = ResultView(['cancelled'])
    count = ResultProjector(client, owners=('tts',)).project_once()
    assert client.deliveries[0]['payloadJson']['text'] == 'Request cancelled.'
    assert count == 1 and len(client.deliveries) == 1 and client.consumed == {'result-0'}, (
        f'cancelled result: projected={count}, deliveries={len(client.deliveries)}, consumed={len(client.consumed)}')


@pytest.mark.parametrize('obsolete', ['failed', 'terminal_error', 'retryable_error'])
def test_attempt_or_obsolete_kinds_are_not_terminal_results(obsolete):
    client = ResultView([obsolete])
    assert ResultProjector(client, owners=('tts',)).project_once() == 0
    assert not client.consumed and not client.deliveries


def test_success_is_eventually_delivered_behind_cancelled_page():
    client = ResultView(['cancelled'] * 200 + ['succeeded'])
    projector = ResultProjector(client, owners=('tts',))
    for _ in range(3):
        projector.project_once()
    assert 'result-200' in client.consumed, (
        f'success behind cancellations: deliveries={len(client.deliveries)}, consumed={len(client.consumed)}')
