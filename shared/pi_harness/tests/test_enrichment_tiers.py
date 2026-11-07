"""Enrichment-role models come from the enrichment tier file, per pipeline, read at each call.

| tier file / roles file                      | outcome                                                            |
| episodes haiku, speech haiku, surveillance sonnet | claude-haiku-5-5, claude-haiku-5-5, claude-sonnet-5-5         |
| meetings haiku, titles haiku                | the meeting summary and the episode title on the same tier          |
| episodes deepseek (X-104)                   | opencode-go-min/deepseek-v4.1-flash at thinking xhigh, both roles  |
| meetings / titles deepseek (X-104.3)         | opencode-go-min/deepseek-v4.1-flash at thinking xhigh              |
| file edited between two calls               | the new tier, no restart                                           |
| missing / bad JSON / not an object / pipeline missing / tier unknown or not a name | EnrichmentConfigError, error_class enrichment_config |
| roles file sets tier, provider or model for an enrichment role | EnrichmentConfigError (even for a reader role lookup) |
| roles file sets thinking for one, on a tier that names its own effort | EnrichmentConfigError naming the tier's effort |
| roles file sets thinking / flags / system_prompt for one, other tiers | applied; the tier still comes from the file |
| roles file's "models" defines the chosen tier (an override or a new name) | EnrichmentConfigError; other tiers may still be defined |
| chosen tier resolves to openai-codex (X-60) | EnrichmentConfigError naming the provider                          |
| enrichment role without a tier              | EnrichmentConfigError; reader roles unaffected                     |
"""
import json
import os

import pytest

from pi_harness import EnrichmentConfigError, PiRunner, enrichment_tier, resolve_role
from pi_harness.roles import ENRICHMENT_ROLES, _JSON_ROLE_PROMPT, _TITLE_ROLE_PROMPT

ROLES = ENRICHMENT_ROLES
TRANSCRIPT = ("transcript_topics", "transcript_mentions")
METADATA = ("meeting_summary", "episode_title")


@pytest.mark.parametrize("pipeline, model", [("episodes", "claude-haiku-5-5"), ("speech", "claude-haiku-5-5"),
                                             ("surveillance", "claude-sonnet-5-5"), ("meetings", "claude-haiku-5-5"),
                                             ("titles", "claude-haiku-5-5")])
def test_each_pipeline_gets_its_tier(pipeline, model):
    tier = enrichment_tier(pipeline)
    for role in ROLES:
        assert {k: resolve_role(role, tier=tier)[k] for k in ("provider", "model")} == {"provider": "claude", "model": model}
    assert PiRunner(ROLES[0], "/tmp", tier=tier).model == model


def test_file_edit_applies_to_the_next_call(enrichment_tiers):
    assert enrichment_tier("surveillance") == "sonnet"
    enrichment_tiers.write_text(json.dumps({"surveillance": "haiku"}))
    assert enrichment_tier("surveillance") == "haiku"


@pytest.mark.parametrize("content", [None, "", "{", "[]", '"haiku"', "{}", '{"episodes": "haiku"}', '{"surveillance": "nope"}',
                                     '{"surveillance": ""}', '{"surveillance": null}', '{"surveillance": 2}'])
def test_unusable_file_is_a_configuration_error(enrichment_tiers, content):
    enrichment_tiers.unlink() if content is None else enrichment_tiers.write_text(content)
    with pytest.raises(EnrichmentConfigError) as error:
        enrichment_tier("surveillance")
    assert error.value.error_class == "enrichment_config"


def test_default_file_is_under_the_config_directory(monkeypatch):
    from pi_harness import roles
    monkeypatch.delenv(roles.ENRICHMENT_FILE_ENV)
    monkeypatch.setenv("HOME", "/home/someone")
    assert roles.enrichment_file() == "/home/someone/.config/tg-tools/enrichment.json"


@pytest.mark.parametrize("field", ["tier", "provider", "model"])
@pytest.mark.parametrize("role", ROLES)
def test_roles_file_cannot_name_a_model_for_an_enrichment_role(tmp_path, monkeypatch, field, role):
    (tmp_path / "roles.json").write_text(json.dumps({role: {field: "haiku", "thinking": "high"}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    with pytest.raises(EnrichmentConfigError) as error:
        enrichment_tier("episodes")
    assert field in str(error.value) and role in str(error.value)
    with pytest.raises(EnrichmentConfigError):
        resolve_role("review")      # the file is wrong for everyone who reads it


@pytest.mark.parametrize("role", ROLES)
def test_the_deepseek_tier_runs_every_enrichment_role_at_xhigh(enrichment_tiers, role):
    enrichment_tiers.write_text(json.dumps({"episodes": "deepseek"}))
    tier = enrichment_tier("episodes")
    assert {k: resolve_role(role, tier=tier)[k] for k in ("provider", "model", "thinking")} == {
        "provider": "opencode-go-min", "model": "deepseek-v4.1-flash", "thinking": "xhigh"}
    runner = PiRunner(role, "/tmp", tier=tier)
    assert (runner.provider, runner.model, runner.thinking) == ("opencode-go-min", "deepseek-v4.1-flash", "xhigh")


@pytest.mark.parametrize("pipeline, role, prompt", [("meetings", "meeting_summary", _JSON_ROLE_PROMPT),
                                                    ("titles", "episode_title", _TITLE_ROLE_PROMPT)])
def test_the_meetings_and_titles_pipelines_run_pi_at_xhigh(tmp_path, monkeypatch, enrichment_tiers, pipeline, role, prompt):
    """X-104.3: a meeting summary and an episode title take their model from the tier file, not a
    second client, and the deepseek tier's effort reaches them."""
    enrichment_tiers.write_text(json.dumps({"meetings": "deepseek", "titles": "deepseek"}))
    log = tmp_path / "pi.jsonl"
    fake = tmp_path / "bin" / "pi"
    fake.parent.mkdir()
    fake.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                    f"with open({str(log)!r}, 'a') as f:\n"
                    "    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n"
                    "print(json.dumps({'title': 'A title'}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    runner = PiRunner(role, str(tmp_path), tier=enrichment_tier(pipeline))
    result = runner.run("the source", allow_tools=False, timeout_s=30)
    argv = json.loads(log.read_text())["argv"]
    assert argv[argv.index("--provider") + 1] == "opencode-go-min"
    assert argv[argv.index("--model") + 1] == "deepseek-v4.1-flash"
    assert argv[argv.index("--thinking") + 1] == "xhigh"
    assert argv[argv.index("--system-prompt") + 1] == prompt
    assert {"--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(argv)
    assert result.ok and json.loads(result.text) == {"title": "A title"}


def test_a_deepseek_notes_call_runs_pi_at_xhigh_with_the_json_prompt(tmp_path, monkeypatch, enrichment_tiers):
    enrichment_tiers.write_text(json.dumps({"episodes": "deepseek"}))
    log = tmp_path / "pi.jsonl"
    fake = tmp_path / "bin" / "pi"
    fake.parent.mkdir()
    fake.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                    f"with open({str(log)!r}, 'a') as f:\n"
                    "    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n"
                    "print(json.dumps({'summary': 's', 'topics': []}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    runner = PiRunner("transcript_topics", str(tmp_path), tier=enrichment_tier("episodes"))
    result = runner.run("the transcript", allow_tools=False, timeout_s=30)
    call = json.loads(log.read_text())
    argv = call["argv"]
    assert argv[0] == "-p" and "--no-session" in argv
    assert argv[argv.index("--provider") + 1] == "opencode-go-min"
    assert argv[argv.index("--model") + 1] == "deepseek-v4.1-flash"
    assert argv[argv.index("--thinking") + 1] == "xhigh"
    assert argv[argv.index("--system-prompt") + 1] == _JSON_ROLE_PROMPT
    assert {"--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(argv)
    assert (call["stdin"], result.ok) == ("the transcript", True)
    assert json.loads(result.text) == {"summary": "s", "topics": []}


@pytest.mark.parametrize("role", ROLES)
def test_roles_file_cannot_set_an_effort_a_tier_owns(tmp_path, monkeypatch, enrichment_tiers, role):
    (tmp_path / "roles.json").write_text(json.dumps({role: {"thinking": "high"}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    enrichment_tiers.write_text(json.dumps({"episodes": "deepseek"}))
    with pytest.raises(EnrichmentConfigError, match="tier 'deepseek' owns the effort"):
        enrichment_tier("episodes")
    with pytest.raises(EnrichmentConfigError, match="thinking"):
        resolve_role(role, tier="deepseek")
    assert resolve_role(role, tier="haiku")["thinking"] == "high"   # the file still moves the other tiers


def test_roles_file_still_sets_effort_and_prompt(tmp_path, monkeypatch):
    (tmp_path / "roles.json").write_text(json.dumps({
        "transcript_topics": {"thinking": "high", "flags": ["--x"], "system_prompt": "P"},
        "models": {"opus": {"model": "claude-opus-next"}}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    tier = enrichment_tier("surveillance")
    topics, mentions = (resolve_role(role, tier=tier) for role in TRANSCRIPT)
    assert (topics["thinking"], topics["flags"], topics["system_prompt"], topics["model"]) == (
        "high", ["--x"], "P", "claude-sonnet-5-5")
    assert (mentions["thinking"], mentions["model"]) == ("high", "claude-sonnet-5-5")
    assert resolve_role("write")["model"] == "claude-opus-next"      # a tier the file did not choose is still its to define


def test_defaults_keep_todays_effort():
    assert (resolve_role("transcript_topics", tier="haiku")["thinking"], resolve_role("transcript_mentions", tier="haiku")["thinking"]) == ("max", "high")
    assert (resolve_role("meeting_summary", tier="haiku")["thinking"], resolve_role("episode_title", tier="haiku")["thinking"]) == ("max", "high")


@pytest.mark.parametrize("role", ROLES)
def test_an_enrichment_role_has_no_default_tier(role):
    assert role in ENRICHMENT_ROLES
    with pytest.raises(EnrichmentConfigError):
        resolve_role(role)
    with pytest.raises(EnrichmentConfigError):
        PiRunner(role, "/tmp")
    assert resolve_role("write")["provider"] == "claude"


@pytest.mark.parametrize("tier, models", [
    ("mine", {"mine": {"provider": "claude", "model": "m"}}),                           # a new name
    ("sonnet", {"sonnet": {"provider": "openai-codex", "model": "gpt-6-luna"}})])      # an override
def test_a_tier_the_roles_file_defines_is_refused(tmp_path, monkeypatch, enrichment_tiers, tier, models):
    (tmp_path / "roles.json").write_text(json.dumps({"models": models}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    enrichment_tiers.write_text(json.dumps({"surveillance": tier}))
    with pytest.raises(EnrichmentConfigError, match=f"redefines model tier '{tier}'"):
        enrichment_tier("surveillance")
    with pytest.raises(EnrichmentConfigError):
        resolve_role("transcript_topics", tier=tier)


def test_a_tier_on_openai_codex_is_refused(enrichment_tiers):
    enrichment_tiers.write_text(json.dumps({"surveillance": "luna", "episodes": "sol", "meetings": "sol",
                                            "titles": "luna"}))
    for pipeline in ("surveillance", "episodes", "meetings", "titles"):
        with pytest.raises(EnrichmentConfigError, match="openai-codex.*X-60"):
            enrichment_tier(pipeline)
