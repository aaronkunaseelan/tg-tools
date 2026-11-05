"""Transcript-role models come from the enrichment tier file, per pipeline, read at each call.

| tier file / roles file                      | outcome                                                            |
| episodes haiku, speech haiku, surveillance sonnet | claude-haiku-5-5, claude-haiku-5-5, claude-sonnet-5-5         |
| file edited between two calls               | the new tier, no restart                                           |
| missing / bad JSON / not an object / pipeline missing / tier unknown or not a name | EnrichmentConfigError, error_class enrichment_config |
| roles file sets tier, provider or model for a transcript role | EnrichmentConfigError (even for a reader role lookup) |
| roles file sets thinking / flags / system_prompt for one | applied; the tier still comes from the file             |
| roles file's "models" defines the chosen tier (an override or a new name) | EnrichmentConfigError; other tiers may still be defined |
| chosen tier resolves to openai-codex (X-60) | EnrichmentConfigError naming the provider                          |
| transcript role without a tier              | EnrichmentConfigError; reader roles unaffected                     |
"""
import json

import pytest

from pi_harness import EnrichmentConfigError, PiRunner, enrichment_tier, resolve_role
from pi_harness.roles import TRANSCRIPT_ROLES

ROLES = ("transcript_topics", "transcript_mentions")


@pytest.mark.parametrize("pipeline, model", [("episodes", "claude-haiku-5-5"), ("speech", "claude-haiku-5-5"),
                                             ("surveillance", "claude-sonnet-5-5")])
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
def test_roles_file_cannot_name_a_model_for_a_transcript_role(tmp_path, monkeypatch, field, role):
    (tmp_path / "roles.json").write_text(json.dumps({role: {field: "haiku", "thinking": "high"}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    with pytest.raises(EnrichmentConfigError) as error:
        enrichment_tier("episodes")
    assert field in str(error.value) and role in str(error.value)
    with pytest.raises(EnrichmentConfigError):
        resolve_role("review")      # the file is wrong for everyone who reads it


def test_roles_file_still_sets_effort_and_prompt(tmp_path, monkeypatch):
    (tmp_path / "roles.json").write_text(json.dumps({
        "transcript_topics": {"thinking": "high", "flags": ["--x"], "system_prompt": "P"},
        "models": {"opus": {"model": "claude-opus-next"}}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    tier = enrichment_tier("surveillance")
    topics, mentions = (resolve_role(role, tier=tier) for role in ROLES)
    assert (topics["thinking"], topics["flags"], topics["system_prompt"], topics["model"]) == (
        "high", ["--x"], "P", "claude-sonnet-5-5")
    assert (mentions["thinking"], mentions["model"]) == ("high", "claude-sonnet-5-5")
    assert resolve_role("write")["model"] == "claude-opus-next"      # a tier the file did not choose is still its to define


def test_defaults_keep_todays_effort():
    assert (resolve_role("transcript_topics", tier="haiku")["thinking"], resolve_role("transcript_mentions", tier="haiku")["thinking"]) == ("max", "high")


@pytest.mark.parametrize("role", ROLES)
def test_transcript_role_has_no_default_tier(role):
    assert role in TRANSCRIPT_ROLES
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
    enrichment_tiers.write_text(json.dumps({"surveillance": "luna", "episodes": "sol"}))
    for pipeline in ("surveillance", "episodes"):
        with pytest.raises(EnrichmentConfigError, match="openai-codex.*X-60"):
            enrichment_tier(pipeline)
