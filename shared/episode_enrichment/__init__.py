"""Transcript enrichment: topic segmentation and evidence-bound references.

The pipeline annotates a script; it never rewrites one. The script document is
published byte-for-byte, and a separate notes document carries the
model-chosen headings, summary and evidence-bound references.
"""
from __future__ import annotations

from .luna import EnrichmentModelError, run_mentions, run_topics
from .source import (AudioSttSource, LayoutSpan, PlainScriptSource, ScriptLayout,
                     ScriptSource, TranscriptSource)
from .paragraphs import ParagraphStream, build_stream, normalize
from .pipeline import (EnrichmentError, EnrichmentResult, PreparedScript, enrich,
                       enrich_script, ensure_script)
from .naming import (EpisodeNames, EpisodeNamingMetadata, build_episode_names,
                     names_from_prefix)
from .render import build_feed_description, build_manifest, render_notes_markdown
from .stt import DEFAULT_STT_MODEL, TranscriptionError, transcribe_audio
from .validate import GateReport, validate_mentions, validate_topics

__all__ = [
    "AudioSttSource",
    "DEFAULT_STT_MODEL",
    "EnrichmentError",
    "EnrichmentModelError",
    "EnrichmentResult",
    "EpisodeNames",
    "EpisodeNamingMetadata",
    "LayoutSpan",
    "ScriptLayout",
    "ScriptSource",
    "TranscriptSource",
    "GateReport",
    "ParagraphStream",
    "PlainScriptSource",
    "PreparedScript",
    "TranscriptionError",
    "build_episode_names",
    "names_from_prefix",
    "build_feed_description",
    "build_manifest",
    "build_stream",
    "enrich",
    "enrich_script",
    "ensure_script",
    "normalize",
    "render_notes_markdown",
    "run_mentions",
    "run_topics",
    "transcribe_audio",
    "validate_mentions",
    "validate_topics",
]
