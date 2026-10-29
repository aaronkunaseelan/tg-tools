"""Synthetic credentials must stay out of every public publication surface."""
import json
from types import SimpleNamespace

import pytest
from episode_enrichment.paragraphs import build_stream
from episode_enrichment.render import (
    build_feed_description, build_manifest, render_notes_markdown,
)
from tts_worker.publisher import publish_episode
from tts_worker.manifest import write_manifest

from tg_tools_common.public_source import public_source_reference


@pytest.mark.parametrize('url', [
    'https://user:password@example.test/source',
    'https://example.test/source?opaque=secret',
    'https://example.test/source#secret',
    'file:///private/source',
])
def test_private_fetch_references_are_omitted(url):
    assert public_source_reference(url) is None


def test_plain_public_reference_is_preserved():
    url = 'https://example.test/article'
    assert public_source_reference(url) == url


MARKER = 'SYNTHETIC_FETCH_CREDENTIAL'
FETCH_URL = 'https://source.example.test/article?X-Amz-Signature=' + MARKER
ANALYSIS = {'summary': 'Synthetic summary.', 'topics': []}


@pytest.mark.parametrize('surface', ['feed', 'notes', 'manifest'])
def test_fetch_credential_is_absent_from_public_documents(surface):
    common = dict(analysis=ANALYSIS, source_url=FETCH_URL)
    if surface == 'feed':
        rendered = build_feed_description(**common)
    else:
        common.update(stream=build_stream('Synthetic source text.'),
                      mentions={'topics': []}, title='Synthetic episode')
        rendered = (render_notes_markdown(**common) if surface == 'notes'
                    else build_manifest(**common, reports={}))
    assert MARKER not in rendered, f'fetch credential is present in public {surface}'


def test_fetch_credential_is_absent_from_episode_install_payload(tmp_path):
    # Use the real public renderer and publication barrier. Replace only
    # external uploads and HTTP, so this test cannot reach B2 or rss-edge.
    source = tmp_path / 'synthetic.txt'
    source.write_text('Synthetic audio and documents.')
    posted = []
    settings = SimpleNamespace(
        rclone_bin='unused', rclone_remote='unused', b2_bucket='unused',
        public_base_url='https://public.example.test',
        api_base_url='https://api.example.test', api_token='synthetic',
        feed_url='https://public.example.test/feed.xml')
    publish_episode(
        title='Synthetic episode',
        description=build_feed_description(analysis=ANALYSIS, source_url=FETCH_URL),
        route='tts', episode_kind='faithful', source_hash='a' * 64,
        source_original=source, source_ext='txt', source_md=source,
        source_meta=source, artifact_md=source, speech_txt=source,
        audio_mp3=source, manifest_json=source, duration_seconds=1,
        settings=settings, fingerprint='b' * 64, runner=lambda argv: None,
        http_get=lambda feed, slug: [],
        http_post=lambda payload: posted.append(payload) or {'ok': True})
    assert len(posted) == 1
    assert MARKER not in json.dumps(posted), 'episode install POST publishes fetch credential'


def test_fetch_credential_is_absent_from_outer_worker_manifest(tmp_path):
    # worker.py supplies sourceRef here independently of shared enrichment;
    # publisher.py uploads this complete outer manifest to the public prefix.
    path = tmp_path / 'public.manifest.json'
    write_manifest(
        path, source={'sourceKey': 'synthetic-source', 'sourceHash': 'a' * 64,
                      'sourceRef': FETCH_URL}, artifacts={}, chunk_receipts=[],
        model_usage=None, voice='synthetic', timings={}, warnings=[])
    assert MARKER not in path.read_text(), 'outer public TTS manifest publishes fetch credential'

