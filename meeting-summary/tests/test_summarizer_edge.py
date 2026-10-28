"""Summaries and OAuth over real HTTP (local stand-ins), plus the response parse table.

strip_json_fences / read_sse_output_text / render_summary_html: pure
call_codex_json against a local responses endpoint
| endpoint answers                         | outcome                                                  |
| SSE deltas forming fenced JSON           | dict; Bearer + chatgpt-account-id, no api key, env key unused |
| 401 body                                 | SummaryError naming HTTP 401                             |
| SSE text that is not JSON                | SummaryError "parse failed"                              |
load_openai_codex_auth
| auth file                                | outcome                                                  |
| fresh oauth entry                        | token used, no HTTP                                      |
| expired entry, token endpoint answers    | refreshed token persisted (refresh rotated)              |
| expired, no refresh token / api_key type | OpenAIAuthError                                          |
"""
import json
import time

import pytest

import openai_auth
from chain_harness import Edge
from openai_auth import OpenAIAuthError, OpenAICodexAuth, load_openai_codex_auth
from summarizer import SummaryError, call_codex_json, read_sse_output_text, render_summary_html, strip_json_fences


def sse(text):
    return b"".join(b"data: " + json.dumps({"type": "response.output_text.delta", "delta": part}).encode() + b"\n"
                    for part in (text[:5], text[5:])) + b'data: {"type":"response.completed","response":{"status":"completed"}}\n' + b"data: [DONE]\n"


def test_parse_table():
    assert strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert read_sse_output_text(sse('{"a":1}').splitlines(keepends=True)) == '{"a":1}'
    html = render_summary_html({"title": "Coordination Meeting", "overview": "Short.", "decisions": ["Do X"],
                                "action_items": ["Alice to send notes"]})
    assert "Coordination Meeting" in html and "Alice to send notes" in html


@pytest.fixture
def edge():
    edge = Edge()
    yield edge
    edge.close()


@pytest.mark.parametrize("status, body, error", [
    (200, sse('```json\n{"ok": true}\n```'), None),
    (401, b'{"detail": "expired"}', "HTTP 401"),
    (200, sse("not json at all"), "parse failed"),
])
def test_call_codex_json(edge, monkeypatch, status, body, error):
    edge.route("POST", r"^/responses$", lambda req: (status, body, "text/event-stream"))
    monkeypatch.setenv("MEETING_SUMMARY_CODEX_RESPONSES_URL", f"{edge.url}/responses")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    call = lambda: call_codex_json(OpenAICodexAuth(token="oauth-token", account_id="acct-123"), model="m",
                                   instructions="Return JSON.", prompt="{}", timeout_sec=10)
    if error:
        with pytest.raises(SummaryError, match=error):
            call()
        return
    assert call() == {"ok": True}
    (req,) = edge.requests
    assert (req.headers["authorization"], req.headers["chatgpt-account-id"]) == ("Bearer oauth-token", "acct-123")
    assert "api-key" not in req.headers and b"sk-should-not-be-used" not in req.body + repr(req.headers).encode()


@pytest.mark.parametrize("entry, token, error", [
    ({"type": "oauth", "access": "token-123", "refresh": "r", "expires": int(time.time() * 1000) + 86_400_000},
     "token-123", None),
    ({"type": "oauth", "access": "expired", "refresh": "refresh-123", "expires": 1}, "fresh-token", None),
    ({"type": "oauth", "access": "", "expires": 1}, None, "refresh token is missing"),
    ({"type": "api_key", "api_key": "not-used"}, None, "not OAuth-backed"),
])
def test_load_openai_codex_auth(edge, monkeypatch, tmp_path, entry, token, error):
    edge.route("POST", r"^/oauth/token$", lambda req: (200, {"access_token": "fresh-token",
                                                              "refresh_token": "refresh-456", "expires_in": 3600}))
    monkeypatch.setattr(openai_auth, "OPENAI_OAUTH_TOKEN_URL", f"{edge.url}/oauth/token")
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"openai-codex": {**entry, "accountId": "acct-123"}}))
    if error:
        with pytest.raises(OpenAIAuthError, match=error):
            load_openai_codex_auth(path)
        return
    assert load_openai_codex_auth(path).token == token
    refreshed = token == "fresh-token"
    assert len(edge.requests) == refreshed
    if refreshed:
        assert b"refresh_token=refresh-123" in edge.requests[0].body
        assert json.loads(path.read_text())["openai-codex"]["refresh"] == "refresh-456"
