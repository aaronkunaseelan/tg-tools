from __future__ import annotations


def reply_anchor_message_id(input_json: dict) -> int:
    return int(input_json.get("statusMessageId") or input_json.get("messageId") or 0)


def accept_reply_markup_payload(callback_data: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Accept", "callback_data": callback_data}],
        ]
    }
