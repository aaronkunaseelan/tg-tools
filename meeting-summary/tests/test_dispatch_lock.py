"""Real PostgreSQL: a review dispatch holds the product row lock for the whole provider call.

While the sender is mid-call, a concurrent metadata correction must wait on a PostgreSQL
row lock (seen in pg_stat_activity), then land as review version 2 after the send.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psycopg

import store
from chain_harness import start_stack
from meeting_gateway_adapter import GatewayAdapter
from tg_gateway.delivery import DeliveryDrain, DeliveryRow, MemoryGatewayState


def test_provider_call_holds_product_mutation_lock():
    stack = start_stack(meeting_schema=True, controller=False)
    try:
        dsn = stack.dsn("meeting")
        saved = store.store_telegram_result(
            dsn, job_id="job-lock", submitted_at="2026-10-03T00:00:00Z", processed_at=datetime.now(UTC), chat_id=8,
            thread_id=None, message_id=2000, sender_user_id=3, file_id="fixture", file_unique_id="fixture-lock",
            file_name="fixture.ogg", mime_type="audio/ogg", file_size_bytes=1, audio_duration_seconds=1,
            transcript_text="Lock fixture", transcript_chunks=[{"text": "Lock fixture"}], asr_provider="mock",
            asr_model="mock", summary={"title": "Initial lock"}, summary_status="ok", summary_model_display="mock",
            summary_error_text=None)
        review = store.load_review_by_note(dsn, saved.meeting_note_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            mutation = []

            class Sender:
                def send_text(self, **kwargs):
                    mutation.append(pool.submit(
                        store.create_metadata_review_version, dsn + " application_name=meet_guard_mutator",
                        current_review=review, edits={"title": "After dispatch"}, source_message_id=2100,
                        created_by_user_id=3))
                    with psycopg.connect(dsn, autocommit=True) as conn:
                        for _ in range(500):
                            row = conn.execute("SELECT wait_event_type FROM pg_stat_activity "
                                               "WHERE application_name='meet_guard_mutator'").fetchone()
                            if row and row[0] == "Lock":
                                break
                            time.sleep(0.01)
                        else:
                            raise AssertionError("mutation did not wait on the product row lock")
                    assert not mutation[0].done()
                    return 9301

            row = DeliveryRow(str(uuid4()), "lock-fixture", "telegram_text", {
                "chatId": 8, "text": "Review", "meetingReviewLink": {
                    "role": "review_message", "reviewId": review.review_id, "reviewVersion": 1}})
            adapter = GatewayAdapter(settings=SimpleNamespace(database_url=dsn))
            drain = DeliveryDrain(sender=Sender(), state=MemoryGatewayState(), pre_dispatch=adapter.pre_dispatch)
            assert drain.drain_row(row).status == "sent"
            result = mutation[0].result(timeout=5)["bundle"].review
        assert (result.review_version, result.metadata_json["title"]) == (2, "After dispatch")
    finally:
        stack.stop()
