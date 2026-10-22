"""review_flow pure table: reviewer replies -> metadata edits, append, or a named rejection.

| reply                                         | parsed                                         |
| "Title: Weekly Planning"                      | title only (partial edit)                      |
| Date / Title / Attendees lines                | all three fields                               |
| "1: d" / "2. t" / "3) a, b" / "3 a, b"        | line-number forms                              |
| attendees with quoted comma + case duplicates | csv split, case-insensitive dedupe             |
| fenced block                                  | fences ignored                                 |
| "1. Title: x" / "Location:" / Title twice     | MetadataParseError                             |
classify_review_reply: metadata before append, short text invalid
callback data: opaque, round-trips, <= 64 bytes; markdown keeps appends in version order
"""
import pytest

from review_flow import (MetadataParseError, build_accept_callback_data, classify_review_reply,
                         parse_accept_callback_data, parse_metadata_edit_block, render_meeting_markdown)


@pytest.mark.parametrize("reply, parsed", [
    ("Title: Weekly Planning", {"title": "Weekly Planning"}),
    ("Date: 2026-05-31\nTitle: Weekly Planning\nAttendees: Alice, Bob",
     {"meeting_date": "2026-05-31", "title": "Weekly Planning", "attendees": ["Alice", "Bob"]}),
    ("1: 2026-05-31", {"meeting_date": "2026-05-31"}),
    ("2. Weekly Planning", {"title": "Weekly Planning"}),
    ("3) Alice, Bob", {"attendees": ["Alice", "Bob"]}),
    ("3 Alice, Bob", {"attendees": ["Alice", "Bob"]}),
    ('Attendees: Alice, "Bob, Jr.", alice, BOB', {"attendees": ["Alice", "Bob, Jr.", "BOB"]}),
    ("```text\nDate: unknown\n```", {"meeting_date": "unknown"}),
    ("Title: A\nTitle: A", {"title": "A"}),
    ("1. Title: Wrong", "Line 1 is Date"),
    ("Location: Room 1", "Unknown metadata field"),
    ("Title: A\nTitle: B", "Conflicting edits"),
])
def test_parse_metadata_edit_block(reply, parsed):
    if isinstance(parsed, str):
        with pytest.raises(MetadataParseError, match=parsed):
            parse_metadata_edit_block(reply)
    else:
        assert parse_metadata_edit_block(reply) == parsed


def test_reply_routing_callback_and_render():
    assert [classify_review_reply(t) for t in ("Date: nope", "1. Title: Demo", "These are extra human notes.",
                                               "too short")] == ["metadata", "metadata", "append", "invalid_short"]
    data = build_accept_callback_data("abcDEF_123", 42)
    parsed = parse_accept_callback_data(data)
    assert (parsed.token, parsed.action, parsed.version) == ("abcDEF_123", "a", 42) and len(data.encode()) <= 64
    md = render_meeting_markdown(
        metadata={"meeting_date": "2026-05-31", "title": "Weekly Planning", "attendees": ["Alice"]},
        notes_json={"overview": "Generated overview.", "action_items": ["Alice sends notes"]},
        appends=[{"append_version": 2, "created_at": "2026-05-31 18:43 UTC", "note_text": "Second note"},
                 {"append_version": 1, "created_at": "2026-05-31 18:42 UTC", "note_text": "First note"}],
        review_status="preview", review_version=3)
    assert 'review_status: "preview"' in md and "Generated overview." in md and "Alice sends notes" in md
    assert md.index("First note") < md.index("Second note")
