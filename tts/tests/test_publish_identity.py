"""Run provenance must not prevent replay of an execution identity."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from tts_common.materialise import materialise
from tts_worker import publisher, worker
from tts_worker.publisher import PublishError, publish_episode

IDENTITY = 'a' * 64
PREFIX = f'https://public.example/tts/episodes/{IDENTITY}/'


def existing_row():
    return dict(feed='tts', slug='first-title', title='Original title', route='tts',
                episode_kind='faithful_read', mp3_url=PREFIX + 'first-title.mp3',
                mp3_size=123, duration='00:01:00', md_url=PREFIX + 'first-title.notes.md',
                speech_text_url=PREFIX + 'first-title.script.md',
                manifest_url=PREFIX + 'first-title.manifest.json', source_md_url=PREFIX + 'source.md')


def publication(tmp_path):
    source = tmp_path / 'changed-run-data'
    source.write_text(json.dumps({'source_ref': 'another/path', 'timestamp': 'new attempt'}))
    settings = SimpleNamespace(public_base_url='https://public.example', feed_url='https://feed.example',
                               api_base_url='https://api.example', api_token='test',
                               rclone_bin='unused', rclone_remote='unused', b2_bucket='unused')
    return dict(title='Regenerated title', description='new description', route='tts',
                episode_kind='faithful_read', source_hash='b' * 64,
                source_original=source, source_ext='txt', source_md=source, source_meta=source,
                artifact_md=source, speech_txt=source, audio_mp3=source, manifest_json=source,
                duration_seconds=999, settings=settings, fingerprint=IDENTITY)


@pytest.mark.parametrize('body', [b'', b'null', b'{}', b'[1]'])
def test_invalid_catalog_response_is_not_empty_success(monkeypatch, body):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return body
    monkeypatch.setattr(publisher.urlrequest, 'urlopen', lambda *a, **kw: Response())
    with pytest.raises(PublishError, match='invalid episode lookup response'):
        publisher._api_get_episodes('https://api.example', 'test', feed='tts', slug=None)


def test_materialised_bytes_ignore_staging_path(tmp_path):
    outputs = []
    for name in ('attempt-one', 'attempt-two'):
        root = tmp_path / name
        root.mkdir()
        source = root / 'document.txt'
        source.write_text('A deterministic source.\n')
        result = materialise(source, root / 'converted')
        outputs.append(tuple(result[key].read_bytes() for key in ('markdown_path', 'meta_path')))
    assert outputs[0] == outputs[1]
    assert str(tmp_path).encode() not in outputs[0][1]
    meta = json.loads(outputs[0][1])
    assert not {'source', 'source_ref', 'declared_mime'} & meta.keys()


def test_existing_identity_returns_before_changed_uploads(tmp_path):
    def no_effect(*args):
        raise AssertionError('existing episode must not upload or POST')
    episode = publish_episode(**publication(tmp_path), runner=no_effect, http_post=no_effect,
                              http_get=lambda feed, slug: [existing_row()])
    assert episode.title == 'Original title'
    assert episode.mp3_size == 123


def test_published_identity_skips_all_generation(tmp_path, monkeypatch):
    from test_progress_recovery import setup_execution
    kwargs = setup_execution(tmp_path, monkeypatch, worker.RetryDecision.GENERATE)
    ep = publisher.PublishedEpisode(slug='first-title', title='Original title', mp3_url=PREFIX+'first-title.mp3',
        md_url=PREFIX+'first-title.notes.md', speech_text_url=PREFIX+'first-title.script.md',
        manifest_url=PREFIX+'first-title.manifest.json', source_md_url=PREFIX+'source.md',
        feed_url='https://feed.example', duration='00:01:00', mp3_size=123)
    monkeypatch.setattr(worker, 'find_published_episode', lambda **kw: ep)
    def no_generation(*args, **kwargs):
        pytest.fail('existing identity must not materialise, generate or synthesize')
    for name in ('materialise', 'resolve_title', 'synthesize_speech', 'decide_retry'):
        monkeypatch.setattr(worker, name, no_generation)
    result = worker._execute_job_in_root(**kwargs)
    assert result['ok'] and result['existingEpisode']
    assert result['durationSeconds'] == 60
    from tts_worker.result_projector import render_notification
    monkeypatch.setenv('TTS_FEED_TITLE', 'Test feed')
    assert render_notification('succeeded', result).startswith('Existing episode in Test feed:')


@pytest.mark.parametrize('phase', ['open', 'read'])
def test_committed_create_timeout_reconciles(tmp_path, monkeypatch, phase):
    lookups = []
    def lookup(*args, **kwargs):
        lookups.append(kwargs['slug'])
        return [] if len(lookups) == 1 else [existing_row()]
    monkeypatch.setattr(publisher, '_api_get_episodes', lookup)
    calls = []
    def urlopen(request, *, timeout):
        calls.append((request.method, timeout))
        if phase == 'open':
            raise TimeoutError('private provider detail')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): raise TimeoutError('private provider detail')
        return Response()
    monkeypatch.setattr(publisher.urlrequest, 'urlopen', urlopen)
    episode = publish_episode(**publication(tmp_path), runner=lambda argv: None)
    assert episode.mp3_size == 123
    assert calls == [('POST', 60)]  # no blind second create
    assert len(lookups) == 2


def test_partial_immutable_conflict_is_visible(tmp_path):
    def conflict(argv):
        raise subprocess.CalledProcessError(1, argv, stderr='immutable file modified')
    with pytest.raises(PublishError) as error:
        publish_episode(**publication(tmp_path), runner=conflict, http_get=lambda *args: [])
    assert error.value.error_class == 'publish_partial_identity_conflict'
    assert 'operator reconciliation' in str(error.value)


def test_partial_without_checkpoint_fails_before_generation(tmp_path, monkeypatch):
    from test_progress_recovery import setup_execution
    kwargs = setup_execution(tmp_path, monkeypatch, worker.RetryDecision.GENERATE)
    for name in ('rclone_bin', 'rclone_remote', 'b2_bucket'):
        setattr(kwargs['settings'], name, 'synthetic')
    monkeypatch.setattr(worker, 'require_empty_identity', publisher.require_empty_identity)
    monkeypatch.setattr(publisher.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='[{"Name":"source.meta.json"}]'))
    monkeypatch.setattr(worker, 'materialise', lambda *a, **kw: pytest.fail('partial identity regenerated'))
    with pytest.raises(worker.TTSJobError) as error:
        worker._execute_job_in_root(**kwargs)
    assert error.value.error_class == 'publish_partial_identity_conflict'
    assert 'operator reconciliation' in str(error.value)
    from tts_worker.failure_policy import classify_outcome
    assert classify_outcome(error.value.error_class) == ('terminal_error', 0)


def test_prepared_marker_without_checkpoint_requires_reconciliation(tmp_path):
    from tts_worker.retry_guard import evaluate_retry_state, PartialPublicationConflict
    (tmp_path / 'publish.prepared.json').write_text(json.dumps(dict(
        identity=IDENTITY, source_hash='b' * 64, slug='first-title', title='Title', pub_date='date')))
    with pytest.raises(PartialPublicationConflict, match='operator reconciliation'):
        evaluate_retry_state(tmp_path, IDENTITY)


@pytest.mark.parametrize('mutation', ['route', 'artifact', 'duplicate'])
def test_identity_lookup_never_accepts_conflicting_catalog(tmp_path, mutation):
    row = existing_row()
    if mutation == 'route': row['route'] = 'other'
    if mutation == 'artifact': row['manifest_url'] = 'https://other.example/file'
    rows = [row, row] if mutation == 'duplicate' else [row]
    with pytest.raises(PublishError) as error:
        publisher.find_published_episode(settings=publication(tmp_path)['settings'], fingerprint=IDENTITY,
            route='tts', episode_kind='faithful_read', http_get=lambda *args: rows)
    assert error.value.error_class == 'publish_identity_conflict'
