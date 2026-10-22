from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class ScribeOrderingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScribeAdmission:
    thread_id: str
    entry_id: str
    operation: str
    sequence: int
    base_revision: int
    input_kind: str

    def to_job_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "threadId": self.thread_id,
            "entryId": self.entry_id,
            "sequence": self.sequence,
            "baseRevision": self.base_revision,
        }


@dataclass(frozen=True)
class ScribeRevision:
    thread_id: str
    entry_id: str
    revision_id: str
    revision: int
    filename: str
    markdown_hash: str
    markdown_text: str


class ScribeStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scribe_threads (
                    thread_id TEXT PRIMARY KEY,
                    bot_identity TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_thread_id INTEGER,
                    root_source_key TEXT NOT NULL UNIQUE,
                    root_message_id INTEGER NOT NULL,
                    created_by_user_id INTEGER,
                    title_hint TEXT,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    current_markdown_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (bot_identity, chat_id, root_message_id)
                );

                CREATE TABLE IF NOT EXISTS scribe_entries (
                    entry_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES scribe_threads(thread_id) ON DELETE CASCADE,
                    source_key TEXT NOT NULL UNIQUE,
                    telegram_message_id INTEGER NOT NULL,
                    reply_to_message_id INTEGER,
                    sequence INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    input_kind TEXT NOT NULL,
                    input_text TEXT,
                    transcript_text TEXT,
                    source_metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finalized_at TEXT,
                    UNIQUE (thread_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS scribe_revisions (
                    revision_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES scribe_threads(thread_id) ON DELETE CASCADE,
                    entry_id TEXT NOT NULL REFERENCES scribe_entries(entry_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    markdown_hash TEXT NOT NULL,
                    markdown_text TEXT NOT NULL,
                    telegram_delivery_id TEXT,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE (thread_id, revision),
                    UNIQUE (entry_id)
                );

                CREATE TABLE IF NOT EXISTS scribe_message_links (
                    bot_identity TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_thread_id INTEGER,
                    telegram_message_id INTEGER NOT NULL,
                    thread_id TEXT NOT NULL REFERENCES scribe_threads(thread_id) ON DELETE CASCADE,
                    entry_id TEXT REFERENCES scribe_entries(entry_id) ON DELETE SET NULL,
                    revision_id TEXT REFERENCES scribe_revisions(revision_id) ON DELETE SET NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (bot_identity, chat_id, telegram_message_id)
                );

                CREATE INDEX IF NOT EXISTS scribe_message_links_thread_idx
                    ON scribe_message_links(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS scribe_entries_thread_status_idx
                    ON scribe_entries(thread_id, status, sequence);
                """
            )

    def create_start_entry(
        self,
        *,
        bot_identity: str,
        chat_id: int,
        message_thread_id: int | None,
        message_id: int,
        sender_user_id: int | None,
        source_key: str,
        input_kind: str,
        input_text: str | None,
        title_hint: str | None,
        source_metadata: dict[str, Any] | None = None,
    ) -> ScribeAdmission:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._admission_for_source_key(conn, source_key)
                if existing is not None:
                    conn.commit()
                    return existing

                now = _now()
                thread_id = _new_id("scr")
                entry_id = _new_id("scre")
                conn.execute(
                    """
                    INSERT INTO scribe_threads (
                        thread_id, bot_identity, chat_id, message_thread_id,
                        root_source_key, root_message_id, created_by_user_id,
                        title_hint, current_revision, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'open', ?, ?)
                    """,
                    (
                        thread_id,
                        bot_identity,
                        int(chat_id),
                        message_thread_id,
                        source_key,
                        int(message_id),
                        sender_user_id,
                        title_hint,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO scribe_entries (
                        entry_id, thread_id, source_key, telegram_message_id,
                        reply_to_message_id, sequence, operation, input_kind,
                        input_text, source_metadata_json, status, created_at
                    )
                    VALUES (?, ?, ?, ?, NULL, 1, 'start', ?, ?, ?, 'pending', ?)
                    """,
                    (
                        entry_id,
                        thread_id,
                        source_key,
                        int(message_id),
                        input_kind,
                        input_text,
                        _json(source_metadata or {}),
                        now,
                    ),
                )
                self._upsert_message_link(
                    conn,
                    bot_identity=bot_identity,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    telegram_message_id=message_id,
                    thread_id=thread_id,
                    entry_id=entry_id,
                    revision_id=None,
                    role="operator_source",
                )
                conn.commit()
                return ScribeAdmission(
                    thread_id=thread_id,
                    entry_id=entry_id,
                    operation="start",
                    sequence=1,
                    base_revision=0,
                    input_kind=input_kind,
                )
            except Exception:
                conn.rollback()
                raise

    def create_append_entry(
        self,
        *,
        bot_identity: str,
        chat_id: int,
        message_thread_id: int | None,
        message_id: int,
        reply_to_message_id: int,
        source_key: str,
        input_kind: str,
        input_text: str | None,
        source_metadata: dict[str, Any] | None = None,
    ) -> ScribeAdmission | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._admission_for_source_key(conn, source_key)
                if existing is not None:
                    conn.commit()
                    return existing

                link = conn.execute(
                    """
                    SELECT thread_id
                    FROM scribe_message_links
                    WHERE bot_identity = ? AND chat_id = ? AND telegram_message_id = ?
                    """,
                    (bot_identity, int(chat_id), int(reply_to_message_id)),
                ).fetchone()
                if link is None:
                    conn.rollback()
                    return None

                thread = conn.execute(
                    """
                    SELECT thread_id, current_revision
                    FROM scribe_threads
                    WHERE thread_id = ? AND status = 'open'
                    """,
                    (str(link["thread_id"]),),
                ).fetchone()
                if thread is None:
                    conn.rollback()
                    return None

                sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM scribe_entries WHERE thread_id = ?",
                        (str(thread["thread_id"]),),
                    ).fetchone()["next_sequence"]
                )
                now = _now()
                entry_id = _new_id("scre")
                conn.execute(
                    """
                    INSERT INTO scribe_entries (
                        entry_id, thread_id, source_key, telegram_message_id,
                        reply_to_message_id, sequence, operation, input_kind,
                        input_text, source_metadata_json, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'append', ?, ?, ?, 'pending', ?)
                    """,
                    (
                        entry_id,
                        str(thread["thread_id"]),
                        source_key,
                        int(message_id),
                        int(reply_to_message_id),
                        sequence,
                        input_kind,
                        input_text,
                        _json(source_metadata or {}),
                        now,
                    ),
                )
                self._upsert_message_link(
                    conn,
                    bot_identity=bot_identity,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    telegram_message_id=message_id,
                    thread_id=str(thread["thread_id"]),
                    entry_id=entry_id,
                    revision_id=None,
                    role="operator_append",
                )
                conn.commit()
                return ScribeAdmission(
                    thread_id=str(thread["thread_id"]),
                    entry_id=entry_id,
                    operation="append",
                    sequence=sequence,
                    base_revision=int(thread["current_revision"]),
                    input_kind=input_kind,
                )
            except Exception:
                conn.rollback()
                raise

    def link_status_message(
        self,
        *,
        bot_identity: str,
        chat_id: int,
        message_thread_id: int | None,
        telegram_message_id: int,
        thread_id: str,
        entry_id: str,
    ) -> None:
        with self._connect() as conn:
            self._upsert_message_link(
                conn,
                bot_identity=bot_identity,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                telegram_message_id=telegram_message_id,
                thread_id=thread_id,
                entry_id=entry_id,
                revision_id=None,
                role="bot_status",
            )

    def mark_entry_failed(self, *, entry_id: str, error_class: str, error_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scribe_entries
                SET status = 'failed',
                    finalized_at = COALESCE(finalized_at, ?),
                    source_metadata_json = ?
                WHERE entry_id = ? AND status != 'completed'
                """,
                (
                    _now(),
                    _json({"errorClass": error_class, "errorText": error_text[:1000]}),
                    entry_id,
                ),
            )

    def finalize_entry(
        self,
        *,
        thread_id: str,
        entry_id: str,
        content_text: str,
        source_metadata: dict[str, Any] | None = None,
    ) -> ScribeRevision:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._revision_for_entry(conn, entry_id)
                if existing is not None:
                    conn.commit()
                    return existing

                entry = conn.execute(
                    """
                    SELECT *
                    FROM scribe_entries
                    WHERE thread_id = ? AND entry_id = ?
                    """,
                    (thread_id, entry_id),
                ).fetchone()
                if entry is None:
                    raise KeyError(f"scribe entry not found: {entry_id}")

                blocker = conn.execute(
                    """
                    SELECT sequence
                    FROM scribe_entries
                    WHERE thread_id = ?
                      AND sequence < ?
                      AND status NOT IN ('completed', 'failed')
                    ORDER BY sequence
                    LIMIT 1
                    """,
                    (thread_id, int(entry["sequence"])),
                ).fetchone()
                if blocker is not None:
                    raise ScribeOrderingError(
                        f"waiting for earlier scribe entry sequence {int(blocker['sequence'])}"
                    )

                now = _now()
                conn.execute(
                    """
                    UPDATE scribe_entries
                    SET transcript_text = ?,
                        source_metadata_json = ?,
                        status = 'completed',
                        finalized_at = ?
                    WHERE entry_id = ?
                    """,
                    (content_text.strip(), _json(source_metadata or {}), now, entry_id),
                )

                thread = conn.execute(
                    "SELECT * FROM scribe_threads WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                if thread is None:
                    raise KeyError(f"scribe thread not found: {thread_id}")

                entries = conn.execute(
                    """
                    SELECT *
                    FROM scribe_entries
                    WHERE thread_id = ? AND status = 'completed'
                    ORDER BY sequence
                    """,
                    (thread_id,),
                ).fetchall()
                revision = int(thread["current_revision"]) + 1
                markdown_text = _render_markdown(thread=thread, entries=entries, revision=revision, updated_at=now)
                markdown_hash = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()
                filename = _revision_filename(thread["created_at"], revision)
                revision_id = _new_id("scrv")
                conn.execute(
                    """
                    INSERT INTO scribe_revisions (
                        revision_id, thread_id, entry_id, revision, filename,
                        markdown_hash, markdown_text, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        thread_id,
                        entry_id,
                        revision,
                        filename,
                        markdown_hash,
                        markdown_text,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE scribe_threads
                    SET current_revision = ?,
                        current_markdown_hash = ?,
                        updated_at = ?
                    WHERE thread_id = ?
                    """,
                    (revision, markdown_hash, now, thread_id),
                )
                conn.commit()
                return ScribeRevision(
                    thread_id=thread_id,
                    entry_id=entry_id,
                    revision_id=revision_id,
                    revision=revision,
                    filename=filename,
                    markdown_hash=markdown_hash,
                    markdown_text=markdown_text,
                )
            except Exception:
                conn.rollback()
                raise

    def link_delivery_message(
        self,
        *,
        bot_identity: str,
        chat_id: int,
        message_thread_id: int | None,
        telegram_message_id: int,
        thread_id: str,
        entry_id: str,
        revision_id: str | None,
        delivery_id: str | None,
        role: str = "bot_markdown_revision",
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_message_link(
                    conn,
                    bot_identity=bot_identity,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    telegram_message_id=telegram_message_id,
                    thread_id=thread_id,
                    entry_id=entry_id,
                    revision_id=revision_id,
                    role=role,
                )
                if revision_id:
                    conn.execute(
                        """
                        UPDATE scribe_revisions
                        SET telegram_message_id = ?,
                            telegram_delivery_id = COALESCE(?, telegram_delivery_id)
                        WHERE revision_id = ?
                        """,
                        (int(telegram_message_id), delivery_id, revision_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _admission_for_source_key(self, conn: sqlite3.Connection, source_key: str) -> ScribeAdmission | None:
        row = conn.execute(
            """
            SELECT e.entry_id, e.thread_id, e.operation, e.sequence, e.input_kind, t.current_revision
            FROM scribe_entries e
            JOIN scribe_threads t ON t.thread_id = e.thread_id
            WHERE e.source_key = ?
            """,
            (source_key,),
        ).fetchone()
        if row is None:
            return None
        return ScribeAdmission(
            thread_id=str(row["thread_id"]),
            entry_id=str(row["entry_id"]),
            operation=str(row["operation"]),
            sequence=int(row["sequence"]),
            base_revision=int(row["current_revision"]),
            input_kind=str(row["input_kind"]),
        )

    def _revision_for_entry(self, conn: sqlite3.Connection, entry_id: str) -> ScribeRevision | None:
        row = conn.execute(
            """
            SELECT thread_id, entry_id, revision_id, revision, filename, markdown_hash, markdown_text
            FROM scribe_revisions
            WHERE entry_id = ?
            """,
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        return ScribeRevision(
            thread_id=str(row["thread_id"]),
            entry_id=str(row["entry_id"]),
            revision_id=str(row["revision_id"]),
            revision=int(row["revision"]),
            filename=str(row["filename"]),
            markdown_hash=str(row["markdown_hash"]),
            markdown_text=str(row["markdown_text"]),
        )

    def _upsert_message_link(
        self,
        conn: sqlite3.Connection,
        *,
        bot_identity: str,
        chat_id: int,
        message_thread_id: int | None,
        telegram_message_id: int,
        thread_id: str,
        entry_id: str | None,
        revision_id: str | None,
        role: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO scribe_message_links (
                bot_identity, chat_id, message_thread_id, telegram_message_id,
                thread_id, entry_id, revision_id, role, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (bot_identity, chat_id, telegram_message_id) DO UPDATE SET
                message_thread_id = excluded.message_thread_id,
                thread_id = excluded.thread_id,
                entry_id = COALESCE(excluded.entry_id, scribe_message_links.entry_id),
                revision_id = COALESCE(excluded.revision_id, scribe_message_links.revision_id),
                role = excluded.role
            """,
            (
                bot_identity,
                int(chat_id),
                message_thread_id,
                int(telegram_message_id),
                thread_id,
                entry_id,
                revision_id,
                role,
                _now(),
            ),
        )


def _render_markdown(*, thread: sqlite3.Row, entries: list[sqlite3.Row], revision: int, updated_at: str) -> str:
    lines = [
        "# Scribe Transcript",
        "",
        f"- Revision: v{revision:03d}",
        f"- Started: {thread['created_at']}",
        f"- Updated: {updated_at}",
        "- Source: Telegram scribe topic",
        f"- Thread: {thread['thread_id']}",
        "",
    ]
    for index, entry in enumerate(entries, start=1):
        label = _entry_label(entry)
        text = str(entry["transcript_text"] or "").strip()
        lines.extend([f"## {index}. {label}", "", text or "(empty)", ""])
    return "\n".join(lines).rstrip() + "\n"


def _entry_label(entry: sqlite3.Row) -> str:
    operation = str(entry["operation"])
    input_kind = str(entry["input_kind"])
    if operation == "start":
        if input_kind == "telegram_text":
            return "Initial Text"
        if input_kind in {"url", "direct_audio_url"}:
            return "Initial Media URL"
        return "Initial Voice Note"
    if input_kind == "telegram_text":
        return "Text Append"
    if input_kind in {"url", "direct_audio_url"}:
        return "Media URL Append"
    return "Voice Note Append"


def _revision_filename(created_at: str, revision: int) -> str:
    stamp = re.sub(r"[^0-9]", "", created_at)[:14] or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"scribe-{stamp}-v{revision:03d}.md"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
