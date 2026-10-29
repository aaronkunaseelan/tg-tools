"""Real edges for tts publication: rclone (local backend, --immutable) and the
rss-edge episode API over HTTP. One publication identity, never two episodes.

| case                                   | edge / filesystem state                     | outcome                                      |
| first publish, then retry              | empty bucket and feed                       | 9 fingerprint keys, one row; retry: no POST  |
| lost install ack                       | POST stores row, answers 502; retry -> 400  | retry converges on the stored row            |
| slug taken by another payload          | row with same slug, other title             | publish_identity_conflict                    |
| key exists with other bytes            | bucket object differs                       | publish_failed (immutable upload)            |
| key exists with the same bytes         | older mtime                                 | publishes (checksum, not mtime)              |
| same source, new converter output      | new identity, other source.md/meta bytes    | second episode; original bytes shared        |
| installed marker for another identity  | state dir pinned elsewhere                  | publish_identity_conflict, nothing uploaded  |
| corrupt installed marker               | "{not json"                                 | quarantined, publish proceeds                |
"""
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from chain_harness import Edge
from tts_worker.publisher import PublishError, publish_episode

FINGERPRINT = "f" * 64
NAMES = ("source_original", "source_md", "source_meta", "artifact_md", "speech_txt", "audio_mp3", "manifest_json")


@pytest.fixture
def edge(tmp_path, monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setenv("RCLONE_CONFIG_CHAINFS_TYPE", "local")
    (tmp_path / "rclone.conf").write_text("")
    (tmp_path / "bucket").mkdir()
    (tmp_path / "state").mkdir()
    edge = Edge()
    yield edge
    edge.close()


def publish(tmp_path, edge, title="Title", fingerprint=FINGERPRINT, state="state", converted=b""):
    files = {}
    for name in NAMES:
        files[name] = tmp_path / f"{name}.bin"
        files[name].write_bytes(b"data-" + name.encode() + (converted if name in ("source_md", "source_meta") else b""))
    settings = SimpleNamespace(rclone_bin="rclone", rclone_remote="chainfs", b2_bucket=str(tmp_path / "bucket"),
                               public_base_url="https://pub.example", api_base_url=edge.url, api_token="t",
                               feed_url="https://feed.example/tts.xml")
    return publish_episode(title=title, description="d", route="x-tts", episode_kind="explained_read",
                           source_hash="a" * 64, source_ext="pdf", duration_seconds=10.0, settings=settings,
                           state_dir=tmp_path / state, fingerprint=fingerprint, **files)


def posts(edge):
    return [r for r in edge.requests if r.method == "POST"]


def test_publish_once_and_retry_returns_installed(tmp_path, edge):
    episode = publish(tmp_path, edge)
    keys = sorted(str(p.relative_to(tmp_path / "bucket")) for p in (tmp_path / "bucket").rglob("*") if p.is_file())
    assert len(keys) == 9 and sum(f"tts/episodes/{FINGERPRINT}/" in k for k in keys) == 8
    (row,) = edge.episodes["tts"]
    assert (row["slug"], row["fingerprint"]) == (episode.slug, FINGERPRINT) and episode.slug.endswith("f" * 12)
    assert publish(tmp_path, edge) == episode and len(posts(edge)) == 1


def test_same_identity_new_run_reuses_existing_bytes(tmp_path, edge):
    first = publish(tmp_path, edge)
    bucket = tmp_path / "bucket"
    before = {str(p.relative_to(bucket)): p.read_bytes() for p in bucket.rglob("*") if p.is_file()}
    (tmp_path / "state2").mkdir()
    second = publish(tmp_path, edge, state="state2", title="Changed run title", converted=b"-new-run")
    assert second == first and len(posts(edge)) == 1
    assert before == {str(p.relative_to(bucket)): p.read_bytes() for p in bucket.rglob("*") if p.is_file()}


def rss_edge_insert(edge, *, first_reply):
    """rss-edge createEpisode: plain INSERT (duplicate slug -> 400 UNIQUE), pub_date stored at ms precision."""
    def insert(req):
        body = req.json()
        if any(r["slug"] == body["slug"] for r in edge.episodes.get("tts", [])):
            return 400, {"error": "D1_ERROR: UNIQUE constraint failed: episodes.feed, episodes.slug"}
        stamp = datetime.fromisoformat(body["pub_date"].replace("Z", "+00:00")).astimezone(UTC)
        stored = {**body, "pub_date": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        edge.episodes.setdefault("tts", []).append(stored)
        return first_reply
    edge.route("POST", r"/api/episodes$", insert)


def test_lost_ack_converges_on_stored_row(tmp_path, edge):
    rss_edge_insert(edge, first_reply=(502, {"error": "bad gateway"}))
    # Even the first call succeeds: 502 is reconciled against the committed row.
    episode = publish(tmp_path, edge)
    assert publish(tmp_path, edge) == episode
    assert len(edge.episodes["tts"]) == 1 and episode.slug == edge.episodes["tts"][0]["slug"]


def test_identity_taken_by_other_route_is_conflict(tmp_path, edge):
    publish(tmp_path, edge)
    edge.episodes["tts"][0]["route"] = "other-route"
    (tmp_path / "state2").mkdir()
    with pytest.raises(PublishError) as conflict:
        publish(tmp_path, edge, state="state2")
    assert conflict.value.error_class == "publish_identity_conflict"


@pytest.mark.parametrize("existing, error", [(b"other bytes", "publish_partial_identity_conflict"), (b"data-source_original", None)])
def test_existing_key_is_never_rewritten(tmp_path, edge, existing, error):
    target = tmp_path / "bucket" / "tts" / "source" / ("a" * 64) / "source-original.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(existing)
    os.utime(target, (0, 0))  # a retry regenerates files: same bytes may carry a different mtime
    if error:
        with pytest.raises(PublishError) as failed:
            publish(tmp_path, edge)
        assert failed.value.error_class == error and not posts(edge)
    else:
        publish(tmp_path, edge)
    assert target.read_bytes() == existing


@pytest.mark.parametrize("marker, error", [
    (json.dumps({"identity": "e" * 64, "episode": {}}), "publish_identity_conflict"),
    ("{not json", None),
])
def test_installed_marker(tmp_path, edge, marker, error):
    (tmp_path / "state" / "publish.installed.json").write_text(marker)
    if error:
        with pytest.raises(PublishError) as caught:
            publish(tmp_path, edge)
        assert caught.value.error_class == error and not list((tmp_path / "bucket").rglob("*.mp3"))
    else:
        publish(tmp_path, edge)
        assert (tmp_path / "state" / "publish.installed.json.quarantined").exists() and len(posts(edge)) == 1


def test_same_source_new_conversion_publishes(tmp_path, edge):
    """A re-read after a converter change: same original bytes, different source.md/meta."""
    publish(tmp_path, edge)
    (tmp_path / "state2").mkdir()
    second = publish(tmp_path, edge, title="Title v2", fingerprint="e" * 64, state="state2", converted=b"-v2")
    assert second.source_md_url.endswith(f"tts/episodes/{'e' * 64}/source.md")
    assert len(edge.episodes["tts"]) == 2
