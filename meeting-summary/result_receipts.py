from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from store import StoredRecording

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS result_receipts (
    result_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    source_kind TEXT,
    source_key TEXT,
    result_kind TEXT NOT NULL,
    storage_done INTEGER NOT NULL DEFAULT 0,
    stored_schema TEXT,
    stored_table TEXT,
    stored_recording_id TEXT,
    stored_source_message_key TEXT,
    telegram_phase TEXT NOT NULL DEFAULT 'pending',
    telegram_stats_message_id INTEGER,
    telegram_output_message_id INTEGER,
    telegram_summary_message_id INTEGER,
    notified_done INTEGER NOT NULL DEFAULT 0,
    consumed_done INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_result_receipts_consumed_done ON result_receipts(consumed_done, updated_at);
"""


@dataclass(frozen=True)
class ResultReceipt:
    result_id: str
    job_id: str
    source_kind: str | None
    source_key: str | None
    result_kind: str
    storage_done: bool
    stored_schema: str | None
    stored_table: str | None
    stored_recording_id: str | None
    stored_source_message_key: str | None
    telegram_phase: str
    telegram_stats_message_id: int | None
    telegram_output_message_id: int | None
    telegram_summary_message_id: int | None
    notified_done: bool
    consumed_done: bool
    last_error: str | None
    created_at: str
    updated_at: str


class ReceiptStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def ensure_receipt(
        self,
        *,
        result_id: str,
        job_id: str,
        source_kind: str | None,
        source_key: str | None,
        result_kind: str,
    ) -> ResultReceipt:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO result_receipts (result_id, job_id, source_kind, source_key, result_kind)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(result_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    source_kind = COALESCE(result_receipts.source_kind, excluded.source_kind),
                    source_key = COALESCE(result_receipts.source_key, excluded.source_key),
                    result_kind = excluded.result_kind,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (result_id, job_id, source_kind, source_key, result_kind),
            )
        return self.get(result_id)

    def get(self, result_id: str) -> ResultReceipt:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM result_receipts WHERE result_id = ?",
                (result_id,),
            ).fetchone()
        if row is None:
            raise KeyError(result_id)
        data = dict(row)
        return ResultReceipt(
            result_id=data["result_id"],
            job_id=data["job_id"],
            source_kind=data["source_kind"],
            source_key=data["source_key"],
            result_kind=data["result_kind"],
            storage_done=bool(data["storage_done"]),
            stored_schema=data["stored_schema"],
            stored_table=data["stored_table"],
            stored_recording_id=data["stored_recording_id"],
            stored_source_message_key=data["stored_source_message_key"],
            telegram_phase=data["telegram_phase"],
            telegram_stats_message_id=data["telegram_stats_message_id"],
            telegram_output_message_id=data["telegram_output_message_id"],
            telegram_summary_message_id=data["telegram_summary_message_id"],
            notified_done=bool(data["notified_done"]),
            consumed_done=bool(data["consumed_done"]),
            last_error=data["last_error"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def record_storage(self, result_id: str, stored: StoredRecording | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE result_receipts
                SET storage_done = 1,
                    stored_schema = ?,
                    stored_table = ?,
                    stored_recording_id = ?,
                    stored_source_message_key = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE result_id = ?
                """,
                (
                    stored.schema if stored else None,
                    stored.table if stored else None,
                    stored.recording_id if stored else None,
                    stored.source_message_key if stored else None,
                    result_id,
                ),
            )

    def record_telegram_phase(
        self,
        result_id: str,
        *,
        phase: str,
        stats_message_id: int | None = None,
        output_message_id: int | None = None,
        summary_message_id: int | None = None,
        notified_done: bool | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE result_receipts
                SET telegram_phase = ?,
                    telegram_stats_message_id = COALESCE(?, telegram_stats_message_id),
                    telegram_output_message_id = COALESCE(?, telegram_output_message_id),
                    telegram_summary_message_id = COALESCE(?, telegram_summary_message_id),
                    notified_done = COALESCE(?, notified_done),
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE result_id = ?
                """,
                (
                    phase,
                    stats_message_id,
                    output_message_id,
                    summary_message_id,
                    int(notified_done) if notified_done is not None else None,
                    result_id,
                ),
            )

    def mark_not_applicable(self, result_id: str) -> None:
        self.record_telegram_phase(result_id, phase="not_applicable", notified_done=True)

    def mark_consumed(self, result_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE result_receipts
                SET consumed_done = 1,
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE result_id = ?
                """,
                (result_id,),
            )

    def set_last_error(self, result_id: str, error_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE result_receipts
                SET last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE result_id = ?
                """,
                (error_text[:2000], result_id),
            )
