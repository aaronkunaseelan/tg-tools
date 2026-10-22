"""Explicit account configuration and configurable artifact ancestor."""

import pytest

from tg_tools_common.config import required_env
from tg_tools_common.runtime_policy import artifact_allowed_roots


def test_required_env_retains_configured_bytes():
    assert required_env("EXAMPLE_SETTING", {"EXAMPLE_SETTING": " configured "}) == " configured "


@pytest.mark.parametrize("env", [{}, {"EXAMPLE_SETTING": ""}, {"EXAMPLE_SETTING": "   "}])
def test_required_env_rejects_missing_or_blank(env):
    with pytest.raises(ValueError, match="EXAMPLE_SETTING"):
        required_env("EXAMPLE_SETTING", env)


def test_workspace_name_is_configurable(tmp_path, monkeypatch):
    workspace = tmp_path / "example-workspace"
    workdir = workspace / "project" / "work"
    monkeypatch.setenv("TG_TOOLS_WORKSPACE_NAME", "example-workspace")
    assert artifact_allowed_roots(workdir) == (workdir, workspace)
    monkeypatch.setenv("TG_TOOLS_WORKSPACE_NAME", "other")
    assert artifact_allowed_roots(workdir) == (workdir,)
