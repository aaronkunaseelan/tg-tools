from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from audio import AudioError, prepare_audio
from config import Settings
from summarizer import SummaryError, summarize_transcript_chunks
from transcribe_endpoint import ASRError, transcribe_chunks


class PipelineError(RuntimeError):
    pass


class LocalSourceMissingError(PipelineError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    source_kind: str
    source_key: str
    local_work_dir: str
    local_source_path: str
    transcript_path: str
    chunks_path: str
    summary_path: str | None
    summary_error_path: str | None
    audio_duration_seconds: float
    chunk_count: int
    transcribe_provider: str
    transcribe_model: str
    summary_status: str
    summary_model: str

    def to_result_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sourceKind": self.source_kind,
            "sourceKey": self.source_key,
            "localWorkDir": self.local_work_dir,
            "localSourcePath": self.local_source_path,
            "transcriptPath": self.transcript_path,
            "chunksPath": self.chunks_path,
            "summaryPath": self.summary_path,
            "summaryErrorPath": self.summary_error_path,
            "audioDurationSeconds": self.audio_duration_seconds,
            "chunkCount": self.chunk_count,
            "transcribeProvider": self.transcribe_provider,
            "transcribeModel": self.transcribe_model,
            "summaryStatus": self.summary_status,
            "summaryModel": self.summary_model,
        }


def _required_text(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise PipelineError(f"missing required input field: {key}")
    return value


def execute_pipeline(settings: Settings, runtime_job: dict[str, Any]) -> ExecutionResult:
    input_json = dict(runtime_job.get("inputJson") or {})
    source_kind = _required_text(input_json, "sourceKind")
    source_key = _required_text(input_json, "sourceKey")
    local_work_dir = Path(_required_text(input_json, "localWorkDir"))
    local_source_path = Path(_required_text(input_json, "localSourcePath"))

    if not local_source_path.exists():
        raise LocalSourceMissingError(f"local source missing: {local_source_path}")

    transcript_dir = local_work_dir / "transcript"
    summary_dir = local_work_dir / "summary"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)

    prepared = prepare_audio(local_source_path, local_work_dir, settings)
    transcription = transcribe_chunks(settings=settings, chunk_paths=prepared.chunk_paths)
    chunk_transcripts = list(transcription.chunks)

    transcript_text = "\n\n".join(item.text for item in chunk_transcripts if item.text.strip()).strip()
    transcript_path = transcript_dir / "transcript.txt"
    transcript_path.write_text(transcript_text + "\n", encoding="utf-8")

    transcript_chunk_rows = [
        {
            "chunk_index": item.chunk_index,
            "name": item.name,
            "text": item.text,
            "language": item.language,
            "elapsed_seconds": item.elapsed_seconds,
        }
        for item in chunk_transcripts
    ]
    chunks_path = transcript_dir / "chunks.json"
    chunks_path.write_text(json.dumps(transcript_chunk_rows, indent=2) + "\n", encoding="utf-8")

    summary_path: Path | None = None
    summary_error_path: Path | None = None
    summary_status = "ok"

    try:
        chunk_summaries, summary_payload = summarize_transcript_chunks(
            settings=settings,
            chunk_texts=[item.text for item in chunk_transcripts if item.text.strip()],
        )
        (summary_dir / "chunk_summaries.json").write_text(
            json.dumps(chunk_summaries, indent=2) + "\n",
            encoding="utf-8",
        )
        summary_path = summary_dir / "final_summary.json"
        summary_path.write_text(json.dumps(summary_payload, indent=2) + "\n", encoding="utf-8")
    except SummaryError as exc:
        summary_status = "failed"
        summary_error_path = summary_dir / "error.json"
        summary_error_path.write_text(
            json.dumps(
                {
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                    "createdAt": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    return ExecutionResult(
        source_kind=source_kind,
        source_key=source_key,
        local_work_dir=str(local_work_dir),
        local_source_path=str(local_source_path),
        transcript_path=str(transcript_path),
        chunks_path=str(chunks_path),
        summary_path=str(summary_path) if summary_path else None,
        summary_error_path=str(summary_error_path) if summary_error_path else None,
        audio_duration_seconds=prepared.duration_seconds,
        chunk_count=len(transcript_chunk_rows),
        transcribe_provider=transcription.provider,
        transcribe_model=transcription.model,
        summary_status=summary_status,
        summary_model=f"openai-codex/{settings.summary_model}",
    )


__all__ = [
    "ASRError",
    "AudioError",
    "ExecutionResult",
    "LocalSourceMissingError",
    "PipelineError",
    "SummaryError",
    "execute_pipeline",
]
