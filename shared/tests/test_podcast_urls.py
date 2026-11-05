import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tg_tools_common.podcast_urls import PODCASTS, STORAGE_HOSTS, display_url, display_url_from_env

ROOT = "https://rss-feeds.stations.work/speech-" + "K" * 22


def test_storage_urls_on_known_hosts_take_the_keyed_root():
    for host in ("rss-feeds.stations.work", "rss-edge.05802.workers.dev", "rss.stations.work"):
        assert display_url(f"https://{host}/tts/episodes/ab/x.mp3", "tts", ROOT) == ROOT + "/episodes/ab/x.mp3"


def test_other_urls_and_missing_roots_are_unchanged():
    for url in ("https://example.com/tts/episodes/x.mp3", "https://rss-feeds.stations.work/webfeeds/a.xml",
                "https://rss-feeds.stations.work/tts-other/x", "https://rss-feeds.stations.work/audio/x.mp3", "", None):
        assert display_url(url, "tts", ROOT) == url
    assert display_url("https://rss-feeds.stations.work/tts/x.mp3", "tts", "") == "https://rss-feeds.stations.work/tts/x.mp3"


def test_root_comes_from_the_environment():
    env = {"TTS_PUBLIC_ROOT": ROOT + "/"}
    assert display_url_from_env("https://rss-feeds.stations.work/tts/x.mp3", "tts", "TTS_PUBLIC_ROOT", env) == ROOT + "/x.mp3"
    assert display_url_from_env("https://rss-feeds.stations.work/tts/x.mp3", "tts", "TTS_PUBLIC_ROOT", {}) == "https://rss-feeds.stations.work/tts/x.mp3"


def test_surveillance_takes_its_keyed_root():
    root = "https://rss-feeds.stations.work/surveillance-" + "K" * 22
    assert PODCASTS["surveillance"] == "surveillance"
    assert display_url("https://rss-feeds.stations.work/surveillance/episodes/ab/x.mp3", "surveillance", root) == \
        root + "/episodes/ab/x.mp3"
    assert display_url("https://rss-feeds.stations.work/audio/episodes/x.mp3", "surveillance", root) == \
        "https://rss-feeds.stations.work/audio/episodes/x.mp3"


def test_unknown_prefix_is_unchanged():
    assert display_url("https://rss-feeds.stations.work/other/x.mp3", "other", ROOT) == "https://rss-feeds.stations.work/other/x.mp3"


def test_mapping_matches_the_rss_feeds_worker():
    """The Worker's half is rss-feeds src/podcasts.js. A host or podcast changed on one side only would give
    Telegram links that 404; this fails instead. Skipped where no rss-feeds checkout exists (public CI)."""
    # On OCI this checkout is ~/tools/tg-tools and rss-feeds ~/workbench/project-folder/rss-edge; the public mirror
    # carries it as rss-edge/ inside this repo.
    here = Path(__file__).resolve()
    candidates = [Path(p) for p in [os.environ.get("RSS_FEEDS_REPO")] if p] + [
        here.parents[4] / "workbench" / "project-folder" / "rss-edge", here.parents[2] / "rss-edge"]
    module = next((c / "src" / "podcasts.js" for c in candidates if (c / "src" / "podcasts.js").exists()), None)
    if module is None or not shutil.which("node"):
        pytest.skip(f"no rss-feeds checkout (tried {', '.join(map(str, candidates))}; set RSS_FEEDS_REPO)")
    script = (f"import {{ STORED_HOSTS, PODCASTS }} from {json.dumps(module.as_uri())};"
              "console.log(JSON.stringify({ hosts: STORED_HOSTS, podcasts: PODCASTS }));")
    worker = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                                       check=True, timeout=30).stdout)
    assert set(worker["hosts"]) == STORAGE_HOSTS
    served = {p["prefix"]: p["name"] for p in worker["podcasts"]}
    assert {prefix: served.get(prefix) for prefix in PODCASTS} == PODCASTS
