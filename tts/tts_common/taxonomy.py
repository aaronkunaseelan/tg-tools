"""Frozen error taxonomy and supported-type matrix for the TTS spine.

Every acquisition/extraction failure maps to exactly one of these classes. Deterministic classes are terminal; transient classes are
retryable. Consumers must not invent parallel dictionaries.
"""

from __future__ import annotations

# Deterministic source/extraction failures: same input always fails.
SOURCE_ACCESS_BLOCKED = "source_access_blocked"
SOURCE_TOO_LARGE = "source_too_large"
SOURCE_TYPE_MISMATCH = "source_type_mismatch"
EXTRACT_INSUFFICIENT_TEXT = "extract_insufficient_text"
EXTRACT_TRUNCATED = "extract_truncated"

# Transient acquisition failure: bounded retry.
SOURCE_FETCH_TRANSIENT = "source_fetch_transient"

# Lease loss: attempt fencing, never an input problem.
LEASE_LOST = "lease_lost"

TERMINAL_SOURCE_CLASSES = frozenset({
    SOURCE_ACCESS_BLOCKED,
    SOURCE_TOO_LARGE,
    SOURCE_TYPE_MISMATCH,
    EXTRACT_INSUFFICIENT_TEXT,
    EXTRACT_TRUNCATED,
})

RETRYABLE_SOURCE_CLASSES = frozenset({SOURCE_FETCH_TRANSIENT})

ALL_CLASSES = TERMINAL_SOURCE_CLASSES | RETRYABLE_SOURCE_CLASSES | {LEASE_LOST}

# Supported-type matrix: effective type is decided by (strong signature,
# declared MIME, suffix) in that precedence. "conflict" rows define the typed
# outcome when a binary claim carries HTML bytes.
SUPPORTED_EFFECTIVE_TYPES = frozenset({
    "pdf", "html", "text", "markdown", "json", "xml", "csv",
    "docx", "pptx", "xlsx",
})

# Known challenge-shell markers (bounded, lowercase substring checks) that
# turn a claimed-binary-but-HTML response into source_access_blocked rather
# than the generic source_type_mismatch.
CHALLENGE_SHELL_MARKERS = (
    "_incapsula_resource",
    "incapsula incident",
    "cf-browser-verification",
    "cf_chl_",
    "checking your browser",
    "attention required! | cloudflare",
    "request unsuccessful. incapsula",
)


def is_challenge_shell(html_bytes: bytes, *, max_scan: int = 65536) -> bool:
    """True when small/known HTML bytes look like an access-challenge shell."""
    text = html_bytes[:max_scan].decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in CHALLENGE_SHELL_MARKERS)
