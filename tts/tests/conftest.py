"""Every test starts with an enrichment tier file (the live ~/.config/tg-tools/enrichment.json is never read).

Tests of a missing or wrong file point TG_TOOLS_ENRICHMENT_FILE elsewhere themselves."""
import json

import pytest

TIERS = {"episodes": "haiku", "speech": "haiku", "surveillance": "sonnet", "meetings": "haiku",
         "titles": "haiku"}


@pytest.fixture(autouse=True)
def enrichment_tiers(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("enrichment") / "enrichment.json"
    path.write_text(json.dumps(TIERS))
    monkeypatch.setenv("TG_TOOLS_ENRICHMENT_FILE", str(path))
    return path
