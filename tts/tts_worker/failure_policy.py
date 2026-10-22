"""Typed failure policy for TTS jobs.

Maps (stage, error_class) to a runtime finalize outcome instead of the old
blanket ``terminal_error``. The runtime bounds retryable jobs by attempt
count and deadline (dead-lettering when exceeded), so ambiguous failures
default to a bounded retry rather than an immediate dead job.

Categories (newsl-derived):
- auth/billing        -> retryable with long backoff (operator repairs creds,
                         job resumes; runtime dead-letters if never fixed)
- transient/ratelimit -> retryable with short backoff
- blocked publish     -> retryable with medium backoff (e.g. Cloudflare 1010)
- deterministic input -> terminal (same input will always fail)
- unknown             -> retryable, finite by runtime attempt cap
"""

from __future__ import annotations

from tts_common import taxonomy

# Deterministic failures: retrying the same input cannot succeed.
TERMINAL_CLASSES = {
    "input_empty",
    "extract_metadata_only",
    "unsupported_format",
    "voice_invalid",
    "invalid_input",
    "audio_pace_implausible",
    "service_configuration_failure",
    "source_too_large_for_reader",
    "source_shape_unsupported",
    "model_budget_exhausted",
    "length_not_allowed",
    # Same identity, different payload: never overwrite.
    "publish_identity_conflict",
} | set(taxonomy.TERMINAL_SOURCE_CLASSES)

# Operator-repairable or transient: bounded retry with class-specific backoff.
RETRYABLE_BACKOFF_SEC = {
    # auth/billing: blocked-style, wait for operator repair
    "model_auth_required": 900,
    "azure_auth_required": 900,
    "gemini_auth_required": 900,
    "publish_auth_required": 900,
    # transient provider/network
    "model_transient": 120,
    "model_rate_limited": 120,
    "rate_limited": 120,
    "source_fetch_failed": 120,
    taxonomy.SOURCE_FETCH_TRANSIENT: 120,
    # lease loss: never an input problem; a fresh attempt may succeed
    taxonomy.LEASE_LOST: 60,
    # cooperative cancellation (the reader observed a lost lease and stopped
    # itself). Same cause as lease_lost, so it gets the same short backoff
    # rather than being laundered into a model-failure class.
    "cancelled": 60,
    # Another cooperative worker holds this source's persistent work root.
    "work_root_busy": 60,
    "publish_transient": 120,
    # blocked at the edge (e.g. Cloudflare 403 / error 1010)
    "publish_blocked": 600,
    # nondeterministic model output; a fresh attempt may produce valid schema
    "model_schema_invalid": 60,
    "plan_too_many_sections": 60,
    "plan_contract_invalid": 60,
    "draft_contract_invalid": 60,
    "check_contract_invalid": 60,
    # hard quality gates still failing after the revision budget; drafting is
    # nondeterministic, so a fresh attempt may produce a faithful script
    "quality_gate_exhausted": 120,
    # provider returned corrupt/unvalidatable audio
    "audio_validation_failed": 120,
}

DEFAULT_BACKOFF_SEC = 300


def effective_error_class(error_class: str, *, reason_code: str = "",
                          recovery_available: bool = False) -> str:
    """Deferral is retryable only with a verified existing recovery path.

    The standalone converter cannot queue or poll OCR. Configuration repair
    requires explicit operator resubmission; no new controller state is needed.
    """
    if error_class == "extract_deferred":
        if recovery_available and reason_code in {"ocr_pending", "ocr_queued"}:
            return error_class
        return "service_configuration_failure"
    return error_class


def classify_outcome(error_class: str, *, reason_code: str = "",
                     recovery_available: bool = False) -> tuple[str, int]:
    """Return existing controller outcome/backoff, gated by recovery evidence."""
    error_class = effective_error_class(error_class, reason_code=reason_code,
                                        recovery_available=recovery_available)
    if error_class in TERMINAL_CLASSES:
        return "terminal_error", 0
    if error_class == "extract_deferred":
        return "retryable_error", 120  # only verified pending/queued recovery
    backoff = RETRYABLE_BACKOFF_SEC.get(error_class, DEFAULT_BACKOFF_SEC)
    return "retryable_error", backoff


# Provider voice validation at admission (before any model/provider spend).
KNOWN_VOICES = {
    "azure": {"alloy", "ash", "ballad", "coral", "echo", "fable",
              "onyx", "nova", "sage", "shimmer", "verse"},
    "gemini": {"achernar", "achird", "algenib", "algieba", "alnilam", "aoede",
               "autonoe", "callirrhoe", "charon", "despina", "enceladus",
               "erinome", "fenrir", "gacrux", "iapetus", "kore", "laomedeia",
               "leda", "orus", "puck", "pulcherrima", "rasalgethi",
               "sadachbia", "sadaltager", "schedar", "sulafat", "umbriel",
               "vindemiatrix", "zephyr", "zubenelgenubi"},
}


def validate_voice(provider: str, voice: str, extra: set[str] | None = None) -> None:
    """Raise ValueError when the voice is not valid for the provider.

    New provider voices can be admitted without a code change via the
    TTS_EXTRA_VOICES env (comma-separated), guarding against table drift.
    """
    import os
    known = KNOWN_VOICES.get(provider)
    if known is None:
        return  # unknown provider: defer to the provider itself
    env_extra = {v.strip().lower() for v in os.environ.get("TTS_EXTRA_VOICES", "").split(",") if v.strip()}
    allowed = known | (extra or set()) | env_extra
    if voice.lower() not in allowed:
        raise ValueError(
            f"voice {voice!r} is not valid for provider {provider!r}; "
            f"known voices: {', '.join(sorted(allowed))}")
