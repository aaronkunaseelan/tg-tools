"""Reader v4: reply parsing, review counts, spoken length, claude and pi turns."""
import json
import os

import pytest

from tts_reader.v4 import ModelThread, finding_count, parse_reply, read_claude, read_pi, spoken_words

GOOD = "=== TITLE ===\nBottles\n=== SYNOPSIS ===\nTwo sentences.\n=== SCRIPT ===\n## Start\nHello there.\n"


@pytest.mark.parametrize("text, rulings, error", [
    (GOOD, False, None),
    (GOOD + "=== RULINGS ===\n1 accepted: real.\n", True, None),
    (GOOD, True, "RULINGS"),
    ("=== TITLE ===\nT\n=== SCRIPT ===\n## A\nx\n", False, "SYNOPSIS"),
    ("=== TITLE ===\nT\n=== SYNOPSIS ===\nS\n=== SCRIPT ===\nNo heading.\n", False, "chapter"),
    ("Preamble.\n" + GOOD, False, None),
])
def test_parse_reply(text, rulings, error):
    if error:
        with pytest.raises(ValueError, match=error):
            parse_reply(text, rulings=rulings)
    else:
        parts = parse_reply(text, rulings=rulings)
        assert parts["TITLE"] == "Bottles" and parts["SCRIPT"].startswith("## Start")


@pytest.mark.parametrize("review, count", [
    ("1. thing\n=== FINDINGS: 1 ===", 1),
    ("Nothing to report.\n=== FINDINGS: 0 ===\n", 0),
    ("=== FINDINGS: 2 ===\nlater\n=== FINDINGS: 3 ===", 3),
])
def test_finding_count(review, count):
    assert finding_count(review) == count


def test_finding_count_missing():
    with pytest.raises(ValueError):
        finding_count("1. a finding with no count line")


def test_spoken_words_excludes_chapter_titles():
    assert spoken_words("## A long chapter title\nOne two three.\n\n## B\nFour.") == 4


def _pi_end(stop="stop", text="hello", error=""):
    message = {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": stop,
               "errorMessage": error, "usage": {"input": 10, "output": 5, "cacheRead": 3, "cacheWrite": 2,
                                                 "totalTokens": 20, "cost": {"total": 0.01}}}
    return json.dumps({"type": "agent_end", "messages": [{"role": "user"}, message]})


def test_read_pi_maps_usage():
    reply = read_pi('{"type": "agent_start"}\n' + _pi_end() + "\n")
    assert reply.text == "hello" and reply.cost_usd == 0.01
    assert reply.usage == {"input_tokens": 10, "cache_creation_input_tokens": 2,
                           "cache_read_input_tokens": 3, "output_tokens": 5, "thinking_tokens": 0}


@pytest.mark.parametrize("stdout", [_pi_end("error", "", "The usage limit has been reached"),
                                    _pi_end("length"), '{"type": "agent_start"}'])
def test_read_pi_rejects_incomplete_replies(stdout):
    with pytest.raises(ValueError):
        read_pi(stdout)


def test_read_claude():
    result = {"type": "result", "result": " script ", "total_cost_usd": 0.2,
              "usage": {"input_tokens": 4, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50,
                        "output_tokens": 30, "output_tokens_details": {"thinking_tokens": 12}}}
    reply = read_claude('{"type": "system"}\n' + json.dumps(result))
    assert reply.text == "script" and reply.usage["thinking_tokens"] == 12
    with pytest.raises(ValueError, match="nope"):
        read_claude(json.dumps({**result, "is_error": True, "result": "nope"}))


@pytest.mark.cli_thread
def test_pi_thread_continues_one_session_file(tmp_path, monkeypatch):
    """A role pointed at a non-claude provider runs `pi`, and its turns share one session file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    fake = bin_dir / "pi"
    fake.write_text(f"""#!/usr/bin/env python3
import json, sys
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
print({_pi_end(text="reply")!r})
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"write": {"provider": "openai-codex", "model": "gpt-6.1-sol"}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(roles))

    thread = ModelThread("write", tmp_path / "evidence", persist=True)
    first = thread.send("write", [{"type": "text", "text": "brief"}, {"type": "text", "text": "source"}])
    thread.send("revise", [{"type": "text", "text": "notes"}])

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert first.text == "reply" and first.model == "gpt-6.1-sol"
    assert calls[0]["stdin"] == "brief\n\nsource"
    for call in calls:
        argv = call["argv"]
        assert argv[argv.index("--session") + 1] == thread.session
        assert argv[argv.index("--mode") + 1] == "json" and "--model" in argv
    receipt = json.loads((tmp_path / "evidence" / "receipts" / "revise.json").read_text())
    assert receipt["provider"] == "openai-codex" and receipt["costUSD"] == 0.01
