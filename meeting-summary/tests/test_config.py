"""validate_settings / load_settings table.

| settings change                 | outcome                                            |
| complete                        | ok                                                 |
| no database url                 | names MEETING_SUMMARY_DATABASE_URL                 |
| ffmpeg missing                  | names MEETING_SUMMARY_FFMPEG_BIN                   |
| unknown venue                   | names MEETING_SUMMARY_RUNTIME_WORKER_VENUE         |
| no claim workload types         | names MEETING_SUMMARY_RUNTIME_CLAIM_WORKLOAD_TYPES |
load_settings reads the controller from RUNTIME_CONTROLLER_API_URL / _BEARER_TOKEN
"""
from pathlib import Path

import pytest

from config import Settings, load_settings, validate_settings

BASE = dict(
    pi_auth_path=Path("/tmp/auth.json"), summary_model="gpt-5.4-mini", transcribe_model="gpt-4o-mini-transcribe",
    transcribe_jobs=3, max_audio_duration_sec=7200, chunk_seconds=600, summary_timeout_sec=180,
    state_dir=Path("/tmp/state"), spool_dir=Path("/tmp/spool"), database_url="postgresql://example",
    retention_days=7, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", runtime_controller_url="http://127.0.0.1:8787",
    runtime_controller_bearer_token=None, runtime_worker_id="meeting-summary-worker-1", runtime_worker_venue="oci",
    runtime_worker_capacity=2, runtime_supported_execution_profiles=("cpu_media",),
    runtime_claim_workload_types=("meeting_summary",), runtime_claim_producer_systems=(),
    runtime_allowed_queue_classes=("foreground", "background"),
    runtime_queue_preference=("foreground", "background", "ops"), runtime_lease_duration_sec=300,
    runtime_heartbeat_interval_sec=30, runtime_result_poll_interval_sec=5)


@pytest.mark.parametrize("change, error", [
    ({}, None),
    ({"database_url": ""}, "MEETING_SUMMARY_DATABASE_URL"),
    ({"ffmpeg_bin": "/missing/ffmpeg"}, "MEETING_SUMMARY_FFMPEG_BIN"),
    ({"runtime_worker_venue": "mac"}, "MEETING_SUMMARY_RUNTIME_WORKER_VENUE"),
    ({"runtime_claim_workload_types": ()}, "MEETING_SUMMARY_RUNTIME_CLAIM_WORKLOAD_TYPES"),
])
def test_validate_settings(change, error):
    settings = Settings(**{**BASE, **change})
    if error:
        with pytest.raises(RuntimeError, match=error):
            validate_settings(settings)
    else:
        validate_settings(settings)


def test_load_settings_reads_shared_controller(monkeypatch, tmp_path):
    for key, value in {"RUNTIME_CONTROLLER_API_URL": "http://controller.test/", "RUNTIME_CONTROLLER_BEARER_TOKEN": "t",
                       "MEETING_SUMMARY_DATABASE_URL": "postgresql://meeting",
                       "MEETING_SUMMARY_STATE_DIR": str(tmp_path / "s")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MEETING_SUMMARY_SPOOL_DIR", raising=False)
    settings = load_settings()
    assert (settings.runtime_controller_url, settings.runtime_controller_bearer_token, settings.database_url) == (
        "http://controller.test", "t", "postgresql://meeting")
    assert settings.spool_dir == tmp_path / "s" / "spool"
