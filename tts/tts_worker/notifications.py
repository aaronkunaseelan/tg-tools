from __future__ import annotations


MAX_FAILURE_DETAIL_CHARS = 160


def render_failure_notification(stage: str, error_class: str, message: str | None = None) -> str:
    text = f"TTS failed at {stage}: {error_class}"
    detail = " ".join(str(message or "").split())[:MAX_FAILURE_DETAIL_CHARS]
    return f"{text} — {detail}" if detail else text
