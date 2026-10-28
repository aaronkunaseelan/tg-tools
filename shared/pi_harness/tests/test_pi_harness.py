"""pi_harness: role resolution and credential pruning (pure tables), plus the
real process boundary (contained subprocess, fake `claude` CLI on PATH).

roles.resolve_role with a TTS_PI_ROLES_FILE override
| override                                 | role                | resolves to                         |
| none                                     | write               | provider claude, own system prompt  |
| write.model                              | write               | model replaced, provider kept       |
| models.haiku + topics tier haiku         | transcript_topics   | claude / next-haiku                 |
| review tier opus                         | review              | claude-opus-5-5, no "tier" key      |
| write provider+model                     | write               | pinned as given                     |
| review tier "nope" / role "missing"      | -                   | ValueError / KeyError               |
runner.pruned_env: OAuth only; OPENAI_* never reaches a child, even if allow-listed
preflight.assert_codex_oauth: chatgpt store with refresh token only; any API key fails
process: stdin piped, timeout kills the group, logs capped; claude turn closed and keyless
"""
import json
import os
import time

import pytest

from pi_harness import PiRunner, resolve_role
from pi_harness.preflight import AuthModeError, assert_codex_oauth
from pi_harness.runner import LOG_OVERFLOW_EXIT, pruned_env, run_contained


@pytest.mark.parametrize("override, role, expected", [
    (None, "write", {"provider": "claude"}),
    ({"write": {"model": "replacement"}}, "write", {"provider": "claude", "model": "replacement"}),
    ({"models": {"haiku": {"model": "next-haiku"}}, "transcript_topics": {"tier": "haiku"}}, "transcript_topics",
     {"provider": "claude", "model": "next-haiku"}),
    ({"review": {"tier": "opus"}}, "review", {"model": "claude-opus-5-5"}),
    ({"write": {"provider": "openai-codex", "model": "pinned"}}, "write", {"provider": "openai-codex", "model": "pinned"}),
])
def test_resolve_role(tmp_path, monkeypatch, override, role, expected):
    if override:
        (tmp_path / "roles.json").write_text(json.dumps(override))
        monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    resolved = resolve_role(role)
    assert {k: resolved[k] for k in expected} == expected
    assert set(resolved) == {"provider", "model", "thinking", "flags", "system_prompt"}
    # Without its own prompt pi falls back to its coding-assistant persona.
    assert role not in ("write", "review") or "coding assistant" not in resolved["system_prompt"].lower()


@pytest.mark.parametrize("override, role, error", [
    ({"review": {"tier": "nope"}}, "review", ValueError), ({}, "missing", KeyError)])
def test_resolve_role_rejects(tmp_path, monkeypatch, override, role, error):
    (tmp_path / "roles.json").write_text(json.dumps(override))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    with pytest.raises(error):
        resolve_role(role)


def test_pruned_env_never_forwards_openai_credentials(monkeypatch):
    monkeypatch.setenv("PATH", "/test/path")
    monkeypatch.setenv("TTS_PI_SECRETX", "drop-me")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-forwarded")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("TTS_PI_ENV_ALLOW", "OPENAI_API_KEY")
    for provider in (None, "openai-codex", "azure-foundry-chat"):
        env = pruned_env(provider) if provider else pruned_env()
        assert env["PATH"] == "/test/path"
        assert not {"TTS_PI_SECRETX", "OPENAI_API_KEY", "OPENAI_BASE_URL"} & set(env)


@pytest.mark.parametrize("store, env_key, error", [
    ({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {"refresh_token": "r"}}, None, None),
    ({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}, None, "expected 'chatgpt'"),
    ({"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-x", "tokens": {"refresh_token": "r"}}, None, "embedded OPENAI_API_KEY"),
    ({"auth_mode": "chatgpt", "tokens": {}}, None, "no refresh token"),
    ({"auth_mode": "chatgpt", "tokens": {"refresh_token": "r"}}, "sk-leaked", "API key present in the environment"),
    (None, None, "no codex auth store"),
])
def test_assert_codex_oauth(tmp_path, monkeypatch, store, env_key, error):
    if env_key:
        monkeypatch.setenv("OPENAI_API_KEY", env_key)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "auth.json"
    if store is not None:
        path.write_text(json.dumps(store))
    if error is None:
        assert assert_codex_oauth(path) == "chatgpt"
    else:
        with pytest.raises(AuthModeError, match=error):
            assert_codex_oauth(path)


def test_run_contained_pipes_kills_and_caps(tmp_path):
    env = {"PATH": os.environ["PATH"]}
    rc, timed_out, stdout, _ = run_contained(["/bin/sh", "-c", "cat"], "hello", str(tmp_path), env, 2)
    assert (rc, timed_out, open(stdout).read()) == (0, False, "hello")

    started = time.monotonic()
    _, timed_out, _, _ = run_contained(["/bin/sh", "-c", "sleep 5"], "", str(tmp_path), env, 1, grace_s=0.1)
    assert timed_out and time.monotonic() - started < 4

    rc, timed_out, stdout, _ = run_contained(["/bin/sh", "-c", "printf 1234567890"], "", str(tmp_path), env, 2, cap_bytes=4)
    assert (rc, timed_out, open(stdout, "rb").read()) == (LOG_OVERFLOW_EXIT, False, b"1234")


def _fake_claude(tmp_path, monkeypatch, body):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(body)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")


def test_claude_turn_is_closed_and_keyless(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, "#!/usr/bin/env python3\nimport json, os, sys\n"
                 "print(json.dumps({'result': json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
                 "'key': os.environ.get('ANTHROPIC_API_KEY')}), 'is_error': False}))\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    result = PiRunner("review", str(tmp_path)).run("the prompt", allow_tools=False, timeout_s=30)
    seen = json.loads(result.text)
    assert (seen["key"], seen["stdin"]) == (None, "the prompt")
    argv = seen["argv"]
    assert argv[:6] == ["-p", "--model", "claude-haiku-5-5", "--effort", "medium", "--setting-sources"]
    assert {"--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"} <= set(argv)
    assert argv[argv.index("--tools") + 1] == "" and "--bare" not in argv


def test_claude_error_result_is_a_failed_turn(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, "#!/bin/sh\necho '{\"result\": \"Not logged in\", \"is_error\": true}'\nexit 1\n")
    result = PiRunner("write", str(tmp_path)).run("x", allow_tools=False, timeout_s=30)
    assert (result.ok, result.error) == (False, "Not logged in")
