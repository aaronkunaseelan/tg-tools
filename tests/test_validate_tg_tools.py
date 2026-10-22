"""validate_tg_tools: one isolated pytest process per package, quick excludes chains, no secrets.

| suite | commands                                                                 |
| quick | every package, `-m "not chain"`                                          |
| full  | shared-package install smoke, then every package including chains       |
Every package directory listed exists and owns a tests/ directory; no command touches a .env file.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_tg_tools  # noqa: E402


def test_suites(capsys):
    quick, full = validate_tg_tools.SUITES["quick"], validate_tg_tools.SUITES["full"]
    assert all("not chain" in c.args for c in quick)
    assert [c.label for c in full] == ["shared-package"] + [c.label for c in quick]
    assert not any("not chain" in c.args for c in full)
    for command in quick:
        assert (command.working_dir / "tests").is_dir(), command.cwd
    validate_tg_tools.list_suites()
    assert ".env" not in capsys.readouterr().out
    assert "agent-secrets" not in Path(validate_tg_tools.__file__).read_text()
