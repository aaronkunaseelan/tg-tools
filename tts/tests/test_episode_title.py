"""A faithful read's episode title is one contained harness call on the `titles` pipeline's tier.

| runner reply / state            | outcome                                                       |
| {"title": "..."} (fenced, quoted) | that title, cleaned                                          |
| job carries a titleHint          | no model call at all                                          |
| run failed (timeout, non-zero)   | TitleError error_class model_transient -> retryable job        |
| reply not JSON, empty or markup title | TitleError error_class model_schema_invalid -> retryable job |
| source with a heading, no reply  | the error, never the heading (no heuristic fallback)           |
"""
import json

import pytest

from pi_harness import StepResult
from tts_worker import episode_title
from tts_worker.episode_title import TITLE_TIMEOUT_S, TitleError, resolve_title
from tts_worker.failure_policy import classify_outcome


class Runner:
    """Records the prompt a real PiRunner would give `pi -p`."""

    def __init__(self, text="", ok=True, error="exit 1"):
        self.text, self.ok, self.error, self.calls = text, ok, error, []

    def run(self, prompt, *, allow_tools, timeout_s, session_name=None, **kwargs):
        self.calls.append({"prompt": prompt, "allow_tools": allow_tools,
                           "timeout_s": timeout_s, "session_name": session_name})
        return StepResult(self.text, 0.1, self.ok, "" if self.ok else self.error)


def source(tmp_path, body="# A page - Wikipedia\n\nbody text"):
    path = tmp_path / "source.md"
    path.write_text(body)
    return path


def test_the_title_is_one_contained_turn_over_the_first_page(tmp_path):
    runner = Runner(json.dumps({"title": "Plastic bottles shed particles"}))
    title = resolve_title(source(tmp_path, "# Heading\n\n" + "x" * 9000), {"title": "Junk | Wikipedia"},
                          runner=runner)
    assert title == "Plastic bottles shed particles"
    [call] = runner.calls
    assert call["allow_tools"] is False and call["timeout_s"] == TITLE_TIMEOUT_S
    assert call["session_name"] == "episode-title"
    assert call["prompt"].startswith("TITLE HINT (may be junk): Junk | Wikipedia")
    assert len(call["prompt"]) < 9000          # the source's first page only


@pytest.mark.parametrize("reply, title", [
    ('```json\n{"title": "  A specific episode  "}\n```', "A specific episode"),
    ('{"title": "\\"Quoted\\""}', "Quoted")])
def test_a_reply_yields_a_clean_title(tmp_path, reply, title):
    assert resolve_title(source(tmp_path), {}, runner=Runner(reply)) == title


def test_an_explicit_hint_never_reaches_a_model(tmp_path):
    runner = Runner('{"title": "from the model"}')
    assert resolve_title(source(tmp_path), {}, runner=runner, title_hint="Asked for this") == "Asked for this"
    assert runner.calls == []


def test_a_failed_call_is_a_named_job_error_not_a_heuristic(tmp_path):
    runner = Runner(ok=False, error="step timed out after 300s (process group terminated)")
    with pytest.raises(TitleError) as error:
        resolve_title(source(tmp_path), {}, runner=runner)
    assert error.value.error_class == "model_transient"
    assert (error.value.error_class in str(error.value)) is False
    assert classify_outcome(error.value.error_class)[0] == "retryable_error"


@pytest.mark.parametrize("reply", ["not json at all", '{"title": ""}', '{"title": "<b>markup</b>"}',
                                   '{"other": "no title"}', "[]"])
def test_an_unusable_reply_is_a_named_job_error(tmp_path, reply):
    with pytest.raises(TitleError) as error:
        resolve_title(source(tmp_path), {}, runner=Runner(reply))
    assert error.value.error_class == "model_schema_invalid"
    assert classify_outcome(error.value.error_class)[0] == "retryable_error"


def test_the_titles_tier_names_the_role_at_its_effort(tmp_path, enrichment_tiers):
    from pi_harness import PiRunner, enrichment_tier, resolve_role

    enrichment_tiers.write_text(json.dumps({"titles": "deepseek"}))
    tier = enrichment_tier("titles")
    assert {k: resolve_role(episode_title.TITLE_ROLE, tier=tier)[k]
            for k in ("provider", "model", "thinking")} == {
        "provider": "opencode-go-min", "model": "deepseek-v4.1-flash", "thinking": "xhigh"}
    runner = PiRunner(episode_title.TITLE_ROLE, str(tmp_path), tier=tier)
    assert (runner.provider, runner.model, runner.thinking) == ("opencode-go-min", "deepseek-v4.1-flash", "xhigh")
