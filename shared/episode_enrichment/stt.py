"""Speech-to-text step, wrapping the ``transcribe.py`` tool.

``transcribe.py`` is a CLI first (qmd2 runs it too): several of its helpers
call ``sys.exit`` on failure, so this module imports it and converts
``SystemExit`` into a typed error the pipeline can classify.

When transcribe.py has a self-hosted endpoint configured (m3-asr, Parakeet
Ultra), the whole file goes there first and Azure is the fallback; the returned
``Transcript`` says which engine ran, so manifests record it.

Default Azure deployment is **mini**. A live comparison across nine
proper nouns (Amadei, Jefferson, Cicero, Cheyenne, Victor Miller, Bernie,
Loving Grace, Anthropic) found mini and full byte-identical on every name; the
only differences were punctuation, capitalization, and filler removal. Full
costs more and is slower for no measured accuracy gain on the thing that
matters here.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import tempfile
from pathlib import Path
from tg_tools_common.progress import current_progress

logger = logging.getLogger(__name__)

DEFAULT_STT_MODEL = "mini"
_MODULE_NAME = "transcribe_audio_tool"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_TRANSCRIBE_PATH = _REPOSITORY_ROOT / "transcribe-audio" / "transcribe.py"
# Kept as a readable default-path receipt; transcribe_path() applies the override.
_TRANSCRIBE_PATH = _DEFAULT_TRANSCRIBE_PATH
_TRANSCRIBE_OVERRIDE_ENV = "TG_TOOLS_TRANSCRIBE_PATH"
_LOADED_PATH: Path | None = None


def transcribe_path() -> Path:
    """Resolve the real tool from the repository, with the explicit override."""
    override = os.environ.get(_TRANSCRIBE_OVERRIDE_ENV)
    return Path(override).expanduser() if override else _DEFAULT_TRANSCRIBE_PATH


class TranscriptionError(RuntimeError):
    """STT failed; carries the stage for retry classification."""

    def __init__(self, message: str, stage: str = "transcribe"):
        super().__init__(message)
        self.stage = stage


def load_transcribe_module():
    """Import ``transcribe.py`` once, by path (its directory is hyphenated)."""
    global _LOADED_PATH
    path = transcribe_path().resolve()
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None and _LOADED_PATH == path:
        return cached
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise TranscriptionError(
            f"cannot load transcription tool at {path}", stage="config")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        _LOADED_PATH = path
    except (OSError, ImportError) as exc:
        sys.modules.pop(_MODULE_NAME, None)
        raise TranscriptionError(
            f"cannot load transcription tool at {path}: {exc}", stage="config") from exc
    return module


class Transcript(str):
    """Formatted transcript text that also names the engine that produced it.

    ``stt_engine`` is the self-hosted model label (for example
    ``parakeet-ultra@fluidaudio-0.18.0``), or None when Azure produced it with
    the requested model.
    """
    stt_engine: str | None = None


def _transcribe_self_hosted(tool, audio_path: str) -> tuple[str, str] | None:
    """The self-hosted endpoint's (raw text, model label), or None to use Azure."""
    config = tool.load_self_hosted_config() if hasattr(tool, "load_self_hosted_config") else None
    if config is None:
        return None
    sink = current_progress()
    occurrence = sink.emit("transcribe_chunks", "started", completed=0, total=1, units="chunks")
    result = tool.transcribe_self_hosted(config, audio_path)
    if result:
        sink.emit("transcribe_chunks", "completed", occurrence=occurrence, completed=1, total=1, units="chunks")
    else:
        sink.emit("transcribe_chunks", "interrupted", occurrence=occurrence)
        logger.warning("self-hosted transcription failed for %s; falling back to Azure", audio_path)
    return result


def transcribe_audio(
    audio_path: str | Path,
    *,
    model: str = DEFAULT_STT_MODEL,
    jobs: int = 4,
    module=None,
) -> Transcript:
    """Transcribe a local audio file to formatted transcript text.

    Returns paragraph-formatted text (blank-line separated), which is exactly
    what ``paragraphs.build_stream`` expects. A configured self-hosted endpoint
    is tried first with the whole file; ``model`` picks the Azure fallback.
    """
    tool = module or load_transcribe_module()
    audio_path = str(audio_path)
    engine = None

    try:
        tool._load_local_env()
        tool.ensure_dependencies()
        result = _transcribe_self_hosted(tool, audio_path)
        if result:
            raw, engine = result
        else:
            config = tool.load_azure_openai_config()
            access_token = tool.fetch_azure_access_token(config)
            resolved = tool.resolve_transcribe_model(model, config)
            logger.info("transcribing %s with Azure deployment %s", audio_path, resolved)

            with tempfile.TemporaryDirectory(prefix="enrich-stt-") as tmpdir:
                sink = current_progress()
                with sink.stage("split_audio"):
                    chunks = tool.prepare_chunks(audio_path, tmpdir)
                occurrence = sink.emit("transcribe_chunks", "started", completed=0, total=len(chunks), units="chunks")
                def on_chunk(completed, total):
                    sink.check()
                    sink.emit("transcribe_chunks", "advanced", occurrence=occurrence, completed=completed, total=total, units="chunks")
                import inspect
                extra = {"on_progress": on_chunk} if "on_progress" in inspect.signature(tool.transcribe_all).parameters else {}
                raw = tool.transcribe_all(config, access_token, chunks, resolved, jobs=jobs, **extra)
                if raw and raw.strip():
                    sink.emit("transcribe_chunks", "completed", occurrence=occurrence, completed=len(chunks), total=len(chunks), units="chunks")
    except SystemExit as exc:
        # transcribe.py logs the real reason to stderr before exiting.
        raise TranscriptionError(
            f"transcription tool exited with status {exc.code}") from exc

    if not raw or not raw.strip():
        raise TranscriptionError("transcription produced no text")

    formatted = tool.format_transcript(raw)
    if not formatted.strip():
        raise TranscriptionError("transcript formatting produced no paragraphs")
    transcript = Transcript(formatted)
    transcript.stt_engine = engine
    return transcript
