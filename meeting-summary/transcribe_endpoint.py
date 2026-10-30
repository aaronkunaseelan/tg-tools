from __future__ import annotations

import concurrent.futures
import importlib.util
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from config import Settings

TRANSCRIBE_PROVIDER = "azure"
TRANSCRIBE_AUDIO_PATH = Path(__file__).resolve().parents[1] / "transcribe-audio" / "transcribe.py"


@dataclass(frozen=True)
class ChunkTranscript:
    chunk_index: int
    name: str
    text: str
    language: str | None
    elapsed_seconds: float


@dataclass(frozen=True)
class TranscriptionBatch:
    provider: str
    model: str
    chunks: tuple[ChunkTranscript, ...]


class ASRError(RuntimeError):
    pass


def _load_transcribe_audio() -> ModuleType:
    if not TRANSCRIBE_AUDIO_PATH.is_file():
        raise ASRError(f"transcribe-audio endpoint implementation missing: {TRANSCRIBE_AUDIO_PATH}")
    module_name = "_tg_tools_transcribe_audio_endpoint"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, TRANSCRIBE_AUDIO_PATH)
    if spec is None or spec.loader is None:
        raise ASRError(f"cannot load transcribe-audio endpoint implementation: {TRANSCRIBE_AUDIO_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _endpoint_config(module: ModuleType):
    try:
        module._load_local_env()
        config = module.load_azure_openai_config()
        access_token = module.fetch_azure_access_token(config)
        return config, access_token
    except SystemExit as exc:
        raise ASRError(f"transcribe endpoint configuration failed with exit code {exc.code}") from exc
    except Exception as exc:
        raise ASRError(f"transcribe endpoint configuration failed: {exc}") from exc


def _transcribe_one(
    *,
    module: ModuleType,
    config,
    access_token: str,
    model: str,
    chunk_path: Path,
    chunk_index: int,
) -> ChunkTranscript:
    started = time.monotonic()
    try:
        raw_text = module.transcribe_chunk(config, access_token, str(chunk_path), model)
    except Exception as exc:
        raise ASRError(f"chunk {chunk_index} failed: {type(exc).__name__} {exc}") from exc
    if raw_text is None:
        raise ASRError(f"chunk {chunk_index} failed at Azure transcription endpoint")
    text = str(module.format_transcript(raw_text) or "").strip()
    if not text:
        raise ASRError(f"chunk {chunk_index} returned empty transcript")
    return ChunkTranscript(
        chunk_index=chunk_index,
        name=chunk_path.name,
        text=text,
        language=None,
        elapsed_seconds=time.monotonic() - started,
    )


def _transcribe_self_hosted(module: ModuleType, chunk_paths: tuple[Path, ...]) -> TranscriptionBatch | None:
    """Every chunk on the self-hosted endpoint, in order (it runs one job at a time).

    None when no endpoint is configured or any chunk fails, so the whole batch goes
    to Azure and one batch never mixes engines.
    """
    config = module.load_self_hosted_config()
    if config is None:
        return None
    chunks: list[ChunkTranscript] = []
    label = config.model
    for index, chunk_path in enumerate(chunk_paths):
        started = time.monotonic()
        result = module.transcribe_self_hosted(config, str(chunk_path))
        text = str(module.format_transcript(result[0]) or "").strip() if result else ""
        if not text:
            return None
        label = result[1]
        chunks.append(ChunkTranscript(chunk_index=index, name=chunk_path.name, text=text, language=None,
                                      elapsed_seconds=time.monotonic() - started))
    return TranscriptionBatch(provider=module.SELF_HOSTED_PROVIDER, model=label, chunks=tuple(chunks))


def transcribe_chunks(*, settings: Settings, chunk_paths: tuple[Path, ...]) -> TranscriptionBatch:
    if not chunk_paths:
        raise ASRError("no audio chunks provided")

    module = _load_transcribe_audio()
    module._load_local_env()
    batch = _transcribe_self_hosted(module, chunk_paths)
    if batch is not None:
        return batch
    config, access_token = _endpoint_config(module)
    try:
        resolved_model = str(module.resolve_transcribe_model(settings.transcribe_model, config))
    except Exception as exc:
        raise ASRError(f"transcribe model resolution failed: {exc}") from exc

    jobs = max(1, settings.transcribe_jobs)
    if jobs == 1 or len(chunk_paths) == 1:
        chunks = tuple(
            _transcribe_one(
                module=module,
                config=config,
                access_token=access_token,
                model=resolved_model,
                chunk_path=chunk_path,
                chunk_index=index,
            )
            for index, chunk_path in enumerate(chunk_paths)
        )
    else:
        results: list[ChunkTranscript | None] = [None] * len(chunk_paths)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(chunk_paths))) as pool:
            futures = [
                pool.submit(
                    _transcribe_one,
                    module=module,
                    config=config,
                    access_token=access_token,
                    model=resolved_model,
                    chunk_path=chunk_path,
                    chunk_index=index,
                )
                for index, chunk_path in enumerate(chunk_paths)
            ]
            for future in concurrent.futures.as_completed(futures):
                chunk = future.result()
                results[chunk.chunk_index] = chunk
        chunks = tuple(chunk for chunk in results if chunk is not None)
        if len(chunks) != len(chunk_paths):
            raise ASRError("transcribe endpoint returned incomplete chunk set")

    return TranscriptionBatch(
        provider=TRANSCRIBE_PROVIDER,
        model=resolved_model,
        chunks=chunks,
    )


__all__ = [
    "ASRError",
    "ChunkTranscript",
    "TranscriptionBatch",
    "transcribe_chunks",
]
