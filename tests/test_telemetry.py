import copy
import json
import re
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'shared'))
from tg_tools_cli.telemetry import collect, write_atomic, configuration, SUMMARY_CONTRACT
from tg_tools_cli.contract_validate import problems

NOW = 1800000000
WORKERS = [('tts-worker', 'tts-worker')]
BOUNDS = {'tts_synthesize': 7200}


def summary():
    return {'schema': 'runtime.health-summary/1', 'at': NOW, 'windowSec': 21600,
            'workloads': [{'workloadType': 'tts_synthesize', 'newestJobAt': '2027-01-15T08:00:00Z',
                           'jobsByStatus': {'dead': 0, 'succeeded': 2}, 'countsComplete': True,
                           'activeComplete': True, 'oldestActiveAgeSec': None,
                           'pastDeadline': False, 'expiredLease': False}],
            'failuresComplete': True, 'failures': []}


def client(value=None, health=None):
    value = summary() if value is None else value
    # Consumer fixtures must validate against the producer's shipped schema.
    assert not problems(SUMMARY_CONTRACT, value)
    return SimpleNamespace(health=lambda: health or {'ok': True, 'runtime': {'workerHeartbeats': [
        {'workerId': 'tts-worker', 'heartbeatFresh': True}]}},
        health_summary=lambda **kwargs: copy.deepcopy(value))


def run(c=None, handoffs=lambda: (False, 42), workers=WORKERS):
    return collect(c or client(), workers, BOUNDS, list(BOUNDS), ['tts'], handoffs, now=NOW)


def check(value, name):
    return next(c for c in value['checks'] if c['name'] == name)


def test_real_output_contract_and_semantic_limits():
    value = run()
    schema = json.loads(Path('/home/ubuntu/tools/mech-d/contracts/telemetry-1.json').read_text())
    assert problems(schema, value) == []
    assert value['state'] == 'green'
    assert value['state'] == ('red' if any(not c['ok'] for c in value['checks']) else 'green')
    assert len(value['checks']) <= 256
    assert len({c['name'] for c in value['checks']}) == len(value['checks'])
    for c in value['checks']:
        assert re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', c['name'])
        assert len(c['reason']) <= 160
    assert {m['name'] for m in value['metrics']} == {'jobs', 'newest_job_age', 'newest_handoff_age'}


def test_controller_unreachable_red_unknown_no_exception_details():
    def unavailable(**kwargs):
        raise ConnectionError('PRIVATE_SECRET')
    value = run(SimpleNamespace(health=unavailable, health_summary=unavailable))
    assert value['state'] == 'red'
    for name in ('controller', 'workers', 'stuck_jobs', 'recent_failures'):
        assert not check(value, name)['ok']
        assert check(value, name)['reason'].startswith('unknown:')
    assert 'PRIVATE_SECRET' not in json.dumps(value)


@pytest.mark.parametrize('field,value', [('oldestActiveAgeSec', 7201), ('pastDeadline', True), ('expiredLease', True)])
def test_stuck_job_each_bound(field, value):
    data = summary()
    data['workloads'][0][field] = value
    assert not check(run(client(data)), 'stuck_jobs')['ok']


@pytest.mark.parametrize('kind', ['job', 'delivery'])
def test_recent_dead(kind):
    data = summary()
    data['failures'] = [{'kind': kind, 'workloadType': 'tts_synthesize', 'errorClass': 'speech_timeout', 'count': 1}]
    value = run(client(data))
    assert value['state'] == 'red'
    assert 'tts_synthesize speech_timeout' in check(value, 'recent_failures')['reason']


def test_worker_stale_missing_and_unreadable_configuration():
    stale = {'ok': True, 'runtime': {'workerHeartbeats': [{'workerId': 'tts-worker', 'heartbeatFresh': False}]}}
    assert not check(run(client(health=stale)), 'workers')['ok']
    assert check(run(workers=[('tts-worker', None)]), 'workers')['reason'].startswith('unknown:')
    assert check(run(workers=[('tts-worker', 'missing')]), 'workers')['reason'].startswith('unknown:')


def test_handoff_stuck_and_read_failure():
    assert not check(run(handoffs=lambda: (True, 1000)), 'handoffs')['ok']
    def failed():
        raise RuntimeError('PRIVATE_TEXT')
    value = run(handoffs=failed)
    assert check(value, 'handoffs')['reason'].startswith('unknown:')
    assert 'PRIVATE_TEXT' not in json.dumps(value)


def test_capped_and_invalid_summary_are_unknown_never_healthy():
    for field in ('countsComplete', 'activeComplete'):
        data = summary()
        data['workloads'][0][field] = False
        value = run(client(data))
        assert check(value, 'stuck_jobs')['reason'].startswith('unknown:')
        if field == 'countsComplete':
            assert not any(m['name'] == 'jobs' for m in value['metrics'])
    data = summary()
    data['failuresComplete'] = False
    assert check(run(client(data)), 'recent_failures')['reason'].startswith('unknown:')
    # The live summary is read after collect() takes `now`, so it is a little newer.
    assert check(run(client({**summary(), 'at': NOW + 0.05})), 'stuck_jobs')['ok']
    for bad in ({}, {**summary(), 'schema': 'wrong'}, {**summary(), 'at': NOW - 301}, {**summary(), 'at': NOW + 301},
                {**summary(), 'workloads': []}):
        c = client()
        c.health_summary = lambda **kwargs: bad
        value = run(c)
        assert check(value, 'stuck_jobs')['reason'].startswith('unknown:')
        assert not value['metrics'] or all(m['name'] == 'newest_handoff_age' for m in value['metrics'])


def test_quiet_has_no_artificial_age_alarm():
    data = summary()
    data['workloads'][0]['newestJobAt'] = None
    value = run(client(data), handoffs=lambda: (False, None))
    assert value['state'] == 'green'
    assert all(m['name'] == 'jobs' for m in value['metrics'])


def test_atomic_replacement(tmp_path):
    path = tmp_path / 'telemetry/tg-tools.json'
    write_atomic(run(), path)
    write_atomic(run(handoffs=lambda: (True, 1000)), path)
    assert json.loads(path.read_text())['state'] == 'red'
    assert list(path.parent.iterdir()) == [path]


def test_effective_config_uses_actual_worker_ids_and_pid_defaults():
    # Product config loaders are the authority, not duplicated telemetry defaults.
    tts = {'TTS_AZURE_TTS_BASE_URL': 'https://example.com', 'TTS_AZURE_TTS_KEY_CMD': 'false',
           'TTS_B2_BUCKET': 'test', 'TTS_FEED_URL': 'https://example.com', 'TTS_FEED_TITLE': 'test',
           'TTS_AZURE_CHAT_BASE_URL': 'https://example.com', 'TTS_AZURE_CHAT_KEY_CMD': 'false',
           'TTS_WORKER_ID': 'custom-tts', 'TTS_WORKER_LEASE_SECONDS': '8000'}
    rss = {'RSS_AUDIO_BASE_URL': 'https://example.com', 'RSS_AUDIO_B2_BUCKET': 'test'}
    def env(unit):
        return (tts if unit == 'tts-worker' else rss if unit.startswith('rss-') else {}, 123)
    workers, bounds, workloads, owners = configuration(env)
    assert workers == [('tts-worker', 'custom-tts'), ('rss-audio-runtime-worker', 'rss-audio-123'),
                       ('rss-audio-transcribe-runtime-worker', 'rss-audio-123-transcribe'),
                       ('meeting-summary-runtime-worker', 'meeting-summary-worker-123')]
    assert bounds['tts_synthesize'] == 8000
    assert bounds['meeting_summary'] == 7380
    assert 'meeting_note' in workloads
    assert 'transcribe-audio' in owners


def test_dry_run_never_writes(monkeypatch, capsys):
    import tg_tools_cli.telemetry as module
    sys.path.insert(0, str(ROOT / 'tg-gateway'))
    monkeypatch.setattr(module, 'configuration', lambda: (WORKERS, BOUNDS, list(BOUNDS), ['tts']))
    monkeypatch.setattr(module, 'collect', lambda *args: run())
    monkeypatch.setattr(module, 'write_atomic', lambda *args: pytest.fail('dry run wrote file'))
    module.run(SimpleNamespace(dry_run=True))
    assert json.loads(capsys.readouterr().out)['schema'] == 'telemetry/1'


def test_handoffs_use_gateway_database_readonly_and_never_fetch_payloads(monkeypatch):
    import psycopg
    from tg_tools_cli.telemetry import handoff_health
    sys.path.insert(0, str(ROOT / 'tg-gateway'))
    calls = []
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, sql):
            assert 'intent_json' not in sql and 'last_error' not in sql
            assert 'EXISTS' in sql and 'LIMIT 1' in sql
        def fetchone(self):
            return True, 901.0
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def cursor(self):
            return Cursor()
    def connect(dsn, **kwargs):
        assert 'default_transaction_read_only=on' in kwargs['options']
        assert 'statement_timeout=1000' in kwargs['options']
        calls.append(dsn)
        return Connection()
    monkeypatch.setattr(psycopg, 'connect', connect)
    assert handoff_health(lambda unit: ({'TG_GATEWAY_DATABASE_URL': 'test-gateway-db'}, 123)) == (True, 901.0)
    assert calls == ['test-gateway-db']


def test_existing_gateway_client_reads_health_and_summary_without_private_list_apis():
    sys.path.insert(0, str(ROOT / 'tg-gateway'))
    from tg_gateway.runtime_client import RuntimeClient
    calls = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps(summary()).encode()
    def urlopen(request, timeout):
        calls.append(request.full_url)
        assert request.method == 'GET' and timeout == 3
        return Response()
    c = RuntimeClient(base_url='http://localhost:8787', timeout=3, urlopen=urlopen)
    c.health()
    value = c.health_summary(workloads=['tts_synthesize'], owners=['tts'])
    assert not problems(SUMMARY_CONTRACT, value)
    assert calls[0].endswith('/healthz')
    assert '/v1/health-summary?' in calls[1] and 'windowSec=21600' in calls[1]


def test_invalid_heartbeat_is_unknown():
    invalid = {'ok': True, 'runtime': {'workerHeartbeats': [{'workerId': 'tts-worker', 'heartbeatFresh': 'yes'}]}}
    assert check(run(client(health=invalid)), 'workers')['reason'].startswith('unknown:')
