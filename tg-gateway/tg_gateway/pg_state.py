"""Gateway state in the ``tg_gateway`` schema: handled updates and delivery receipts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row


class PostgresHandledUpdateStore:
    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    def claim_update(self, update_uid: str, *, source_system: str = "telegram") -> bool:
        with psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                """
                INSERT INTO tg_gateway.handled_updates (update_uid, source_system, disposition)
                VALUES (%s, %s, 'uncertain')
                ON CONFLICT (update_uid) DO NOTHING
                RETURNING update_uid
                """,
                (update_uid, source_system),
            ).fetchone()
        return row is not None

    def disposition(self, update_uid: str) -> str | None:
        with psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT disposition FROM tg_gateway.handled_updates WHERE update_uid = %s",
                (update_uid,)).fetchone()
        return row[0] if row else None

    def mark_handled(self, update_uid: str) -> None:
        with psycopg.connect(self._dsn) as conn:
            conn.execute(
                "UPDATE tg_gateway.handled_updates SET disposition = 'handled' WHERE update_uid = %s",
                (update_uid,))


class PostgresGatewayState:
    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    def sent_message_id(self, delivery_key: str, *, delivery_id: str) -> int | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT telegram_message_id, delivery_key
                  FROM tg_gateway.sent_messages
                 WHERE delivery_id = %s::uuid
                """,
                (delivery_id,),
            ).fetchone()
        if row is not None and row["delivery_key"] != delivery_key:
            raise ValueError("delivery receipt identity mismatch")
        return int(row["telegram_message_id"]) if row is not None else None

    def begin_dispatch(self, *, delivery_id: str, delivery_key: str, dispatch_token: str) -> bool:
        # This commit precedes the provider call. No automatic expiry: a crashed
        # sender may have succeeded externally, regardless of its controller lease.
        with psycopg.connect(self._dsn) as conn:
            row = conn.execute("""
                INSERT INTO tg_gateway.delivery_dispatches (delivery_key, delivery_id, dispatch_token)
                VALUES (%s, %s::uuid, %s::uuid)
                ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_key
                """, (delivery_key, delivery_id, dispatch_token)).fetchone()
        return row is not None

    def clear_dispatch(self, *, delivery_id: str, delivery_key: str, dispatch_token: str) -> None:
        with psycopg.connect(self._dsn) as conn:
            conn.execute("""DELETE FROM tg_gateway.delivery_dispatches
                            WHERE delivery_id = %s::uuid AND delivery_key = %s AND dispatch_token = %s::uuid""",
                         (delivery_id, delivery_key, dispatch_token))

    def remember_sent(
        self,
        *,
        delivery_id: str,
        delivery_key: str,
        chat_id: int,
        topic_id: int | None,
        telegram_message_id: int,
    ) -> None:
        with psycopg.connect(self._dsn) as conn:
            conn.execute(
                """
                INSERT INTO tg_gateway.sent_messages (
                    delivery_id,
                    chat_id,
                    topic_id,
                    telegram_message_id,
                    delivery_key
                )
                VALUES (%s::uuid, %s, %s, %s, %s)
                ON CONFLICT (delivery_id) DO NOTHING
                """,
                (delivery_id, chat_id, topic_id, telegram_message_id, delivery_key),
            )

    def set_flood_wait(self, chat_id: int, retry_after_seconds: int) -> None:
        retry_after = datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        with psycopg.connect(self._dsn) as conn:
            conn.execute(
                """
                INSERT INTO tg_gateway.flood_state (chat_id, retry_after_ts)
                VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    retry_after_ts = EXCLUDED.retry_after_ts,
                    updated_at = now()
                """,
                (chat_id, retry_after),
            )
