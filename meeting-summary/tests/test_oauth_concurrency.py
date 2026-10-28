"""Overlap a real refresh write with a second writer, using a synthetic store."""
import io
import json
import time

import openai_auth


import pytest


@pytest.mark.parametrize('rotate_same_provider', [False, True])
def test_refresh_preserves_a_concurrent_provider_rotation(tmp_path, monkeypatch, rotate_same_provider):
    path = tmp_path / 'synthetic-auth.json'
    initial = {
        'openai-codex': {'type': 'oauth', 'accountId': 'synthetic-account',
                         'access': 'synthetic-old', 'refresh': 'synthetic-refresh',
                         'expires': int(time.time() * 1000) - 1000},
        'other-provider': {'key': 'synthetic-before'},
    }
    path.write_text(json.dumps(initial))
    path.chmod(0o600)

    def response_during_rotation(request, **kwargs):
        current = json.loads(path.read_text())
        current['other-provider'] = {'key': 'synthetic-concurrent-rotation'}
        current['new-provider'] = {'key': 'synthetic-new-provider'}
        if rotate_same_provider:
            current['openai-codex'].update(access='synthetic-newer', expires=int(time.time() * 1000) + 7200000)
        path.write_text(json.dumps(current))
        return io.BytesIO(json.dumps({
            'access_token': 'synthetic-refreshed',
            'refresh_token': 'synthetic-refreshed-refresh', 'expires_in': 7200,
        }).encode())

    monkeypatch.setattr(openai_auth.urllib.request, 'urlopen', response_during_rotation)
    result = openai_auth.load_openai_codex_auth(path)
    assert result.token == ('synthetic-newer' if rotate_same_provider else 'synthetic-refreshed')
    stored = json.loads(path.read_text())
    assert (stored['other-provider']['key'] == 'synthetic-concurrent-rotation'
            and 'new-provider' in stored), 'OAuth refresh overwrote concurrent provider edits'

