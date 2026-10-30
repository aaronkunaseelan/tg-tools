from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from tg_tools_common.lease import AttemptHeartbeat, LeaseLost
from tg_tools_common.progress import NullProgress, ProgressReporter, current_progress, use_progress

from tts_common.classify import CHATGPT_SHARE_RE, classify_input
from tts_common.compile_faithful import compile_artifact
from tts_common.convert import CONVERTER_VERSION
from tts_common.faithful_cleanup import CLEANUP_VERSION, clean_faithful_markdown, episode_description
from tts_common.materialise import materialise
from tts_common.speech_text import artifact_to_speech_text
from tts_common import typed_fetch

from .acquire import AcquireError, acquire_source
from episode_enrichment import (EpisodeNamingMetadata, PlainScriptSource,
                                ScriptSource, build_episode_names, enrich,
                                names_from_prefix)

from .episode_title import heuristic_title, resolve_title
from tts_reader.adapter import compile_explained_artifact_pi
from tts_reader.errors import ExplainError
from .config import Settings, load_settings
from .failure_policy import classify_outcome, effective_error_class, speech_engine, validate_voice
from .fingerprint import RoleMapError, execution_fingerprint, reader_role_map
from .generation_checkpoint import (
    GenerationCheckpointIdentityConflict,
    GenerationCheckpointInvalid,
    write_generation_checkpoint,
)
from .manifest import write_manifest
from .publisher import (PublishError, find_published_episode, pinned_artifact_prefix,
                        publish_episode, require_empty_identity)
from .retry_guard import (
    CheckpointRecoveryError,
    RetryDecision,
    RetryGuardConflict,
    archive_superseded_publication,
    decide_retry,
    quarantine_generation_checkpoint,
    result_from_checkpoint,
    result_from_episode,
)
from .runtime_client import RuntimeClient, RuntimeClientError
from .speech_gemini import (
    AzureSpeechProvider,
    GeminiSpeechProvider,
    SelfHostedSpeechProvider,
    SpeechError,
    synthesize_with_fallback,
)
from .work_state import WorkRootError, acquire_work_root, ensure_work_state

logger = logging.getLogger(__name__)


class TTSJobError(RuntimeError):
    def __init__(self, stage: str, error_class: str, message: str, *,
                 disposition: str = "", reason_code: str = "",
                 recovery_available: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.error_class = error_class
        self.disposition = disposition
        self.reason_code = reason_code
        self.recovery_available = recovery_available


def _safe_title(source_md: Path, meta: dict[str, Any]) -> str:
    return heuristic_title(source_md, meta)


def _metadata_creators(meta: dict[str, Any]) -> tuple[str, ...]:
    raw = meta.get("authors") or meta.get("creators") or meta.get("author") or ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    values = []
    for item in raw:
        value = item.get("name") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return tuple(values)


def execute_job(
    settings: Settings,
    input_json: dict[str, Any],
    *,
    attempt_id: str,
    source_fetcher: Any | None = None,
    gemini_post: Any | None = None,
    openai_post: Any | None = None,
    pi_runner_factory: Any | None = None,
    rclone_runner: Any | None = None,
    media_runner: Any | None = None,
    episode_post: Any | None = None,
    episode_get: Any | None = None,
    cancel_check: Any | None = None,
) -> dict[str, Any]:
    source_key = str(input_json.get("sourceKey") or "job")
    try:
        with acquire_work_root(settings.work_dir, source_key) as work:
            return _execute_job_in_root(
                settings,
                input_json,
                work=work,
                source_key=source_key,
                attempt_id=attempt_id,
                source_fetcher=source_fetcher,
                gemini_post=gemini_post,
                openai_post=openai_post,
                pi_runner_factory=pi_runner_factory,
                rclone_runner=rclone_runner,
                media_runner=media_runner,
                episode_post=episode_post,
                episode_get=episode_get,
                cancel_check=cancel_check,
            )
    except WorkRootError as exc:
        raise TTSJobError("work_root", exc.error_class, str(exc)) from exc


def _execute_job_in_root(
    settings: Settings,
    input_json: dict[str, Any],
    *,
    work: Path,
    source_key: str,
    attempt_id: str,
    source_fetcher: Any | None,
    gemini_post: Any | None,
    openai_post: Any | None,
    pi_runner_factory: Any | None,
    rclone_runner: Any | None,
    media_runner: Any | None,
    episode_post: Any | None,
    episode_get: Any | None,
    cancel_check: Any | None,
) -> dict[str, Any]:
    started = time.time()
    _cancel = cancel_check or (lambda: None)
    workload = str(input_json.get("workloadType") or input_json.get("mode") or "")
    x_mode = workload == "x_tts_explain" or (str(input_json.get("mode")) == "explain" and str(input_json.get("promptVersion", "")).startswith("x_tts"))
    stage = "admission"
    try:
        if x_mode and "length" in input_json:
            raise TTSJobError("admission", "length_not_allowed", "x-tts explain does not accept length")
        # Admission: reject invalid voices before any acquisition/model spend.
        voice = str(input_json.get("voice") or settings.default_voice)
        engine = speech_engine(settings.speech_provider, voice)
        try:
            validate_voice(engine, voice)
        except ValueError as exc:
            raise TTSJobError("admission", "voice_invalid", str(exc)) from exc
        _cancel()
        stage = "acquire"
        try:
            acquired = acquire_source(input_json, work, fetcher=source_fetcher)
        except AcquireError as exc:
            raise TTSJobError("acquire", exc.error_class, str(exc)) from exc
        stage = "fingerprint"
        try:
            roles = reader_role_map() if x_mode else None
            fp = execution_fingerprint(
                source_hash=acquired.source_hash,
                mode="explain" if x_mode else "read",
                requested_length=str(input_json.get("length") or "standard"),
                voice=voice,
                speech_provider=engine,
                speech_model=(settings.azure_tts_model if engine == "azure"
                              else settings.self_hosted_speech_model if engine == "self-hosted"
                              else GeminiSpeechProvider.model),
                reader_roles=roles,
                transform_contract_config_version=CONVERTER_VERSION,
            )
        except RoleMapError as exc:
            raise TTSJobError("fingerprint", "reader_role_map_failed", str(exc)) from exc

        stage = "publish_lookup"
        _cancel()
        try:
            existing = find_published_episode(
                settings=settings, fingerprint=fp, route="x-tts" if x_mode else "tts",
                episode_kind="explained_read" if x_mode else "faithful_read", http_get=episode_get)
        except PublishError as exc:
            raise TTSJobError(stage, exc.error_class, str(exc)) from exc
        if existing is not None:
            current_progress().emit("publication_checkpoint", "reused", reason="checkpoint_reused")
            return result_from_episode(existing, source_key=source_key, x_mode=x_mode, existing=True)

        stage = "checkpoint"
        try:
            retry = decide_retry(work, fp)
        except RetryGuardConflict as exc:
            raise TTSJobError("checkpoint", exc.error_class, str(exc)) from exc
        if retry.decision is RetryDecision.C2_RECOVERY:
            try:
                quarantine_generation_checkpoint(work, attempt_id)
            except CheckpointRecoveryError as exc:
                raise TTSJobError(
                    "checkpoint", "generation_checkpoint_recovery_failed", str(exc)
                ) from exc
        elif retry.decision is RetryDecision.SUPERSEDED:
            # A completed publication for this source exists under an older
            # identity (voice, provider or transform contract changed). Its
            # keys are identity-addressed and disjoint from ours, so archive
            # its markers and generate the new episode rather than treating
            # the source as permanently unpublishable.
            logger.warning(
                "archiving a completed prior publication under a different "
                "identity; regenerating for %s", fp)
            try:
                archive_superseded_publication(work, attempt_id)
            except CheckpointRecoveryError as exc:
                raise TTSJobError(
                    "checkpoint", "generation_checkpoint_recovery_failed", str(exc)
                ) from exc
        elif retry.decision is RetryDecision.INSTALLED:
            assert retry.checkpoint is not None and retry.episode is not None
            current_progress().emit("publication_checkpoint", "reused", reason="checkpoint_reused")
            return result_from_checkpoint(retry.checkpoint, retry.episode, source_key=source_key)
        elif retry.decision is RetryDecision.PUBLISH_FROM_G:
            assert retry.checkpoint is not None
            checkpoint = retry.checkpoint
            current_progress().emit("generation_checkpoint", "reused", reason="checkpoint_reused")
            stage = "publish"
            publication = checkpoint.publication
            artifacts = checkpoint.artifacts
            try:
                episode = publish_episode(
                    fingerprint=checkpoint.identity,
                    title=publication["title"],
                    description=publication["description"],
                    route=publication["route"],
                    episode_kind=publication["episodeKind"],
                    source_hash=checkpoint.source["hash"],
                    source_original=artifacts["sourceOriginal"].path,
                    source_ext=checkpoint.source["ext"],
                    source_md=artifacts["sourceMarkdown"].path,
                    source_meta=artifacts["sourceMeta"].path,
                    artifact_md=artifacts["artifactMarkdown"].path,
                    speech_txt=artifacts["speechText"].path,
                    audio_mp3=artifacts["audioMp3"].path,
                    manifest_json=artifacts["manifestJson"].path,
                    duration_seconds=publication["durationSeconds"],
                    primary_document=(artifacts.get("primaryDocument").path
                                      if "primaryDocument" in artifacts else None),
                    script_document=(artifacts.get("publicScript").path
                                     if "publicScript" in artifacts else artifacts["speechText"].path),
                    notes_document=(artifacts.get("publicNotes").path
                                    if "publicNotes" in artifacts else
                                    (artifacts.get("primaryDocument").path
                                     if "primaryDocument" in artifacts else artifacts["artifactMarkdown"].path)),
                    artifact_prefix=(artifacts["publicNotes"].path.name[:-len(".notes.md")]
                                     if "publicNotes" in artifacts and
                                     artifacts["publicNotes"].path.name.endswith(".notes.md")
                                     else None),
                    settings=settings,
                    runner=rclone_runner,
                    http_post=episode_post,
                    http_get=episode_get,
                    state_dir=work,
                    cancel=_cancel,
                )
            except PublishError as exc:
                raise TTSJobError("publish", exc.error_class, str(exc)) from exc
            return result_from_checkpoint(checkpoint, episode, source_key=source_key)

        stage = "publish_lookup"
        _cancel()
        try:
            require_empty_identity(settings, fp)
        except PublishError as exc:
            raise TTSJobError(stage, exc.error_class, str(exc)) from exc

        stage = "materialise"
        try:
            if CHATGPT_SHARE_RE.search(acquired.source_ref):
                mat = materialise(
                    acquired.path,
                    work / "materialised",
                    acquired.mime_type,
                    source_ref=acquired.source_ref,
                )
            else:
                mat = materialise(acquired.path, work / "materialised", acquired.mime_type)
            source_md = Path(mat["markdown_path"])
            source_meta = Path(mat["meta_path"])
            meta = json.loads(source_meta.read_text(encoding="utf-8"))
            if not source_md.read_text(encoding="utf-8").strip():
                raise TTSJobError("materialise", "extract_metadata_only", "empty extraction")
            # Insufficient/challenge-shell extraction never reaches a model.
            try:
                typed_fetch.check_extraction_sufficiency(source_md.read_text(encoding="utf-8"))
            except typed_fetch.TypedFetchError as exc:
                raise TTSJobError("materialise", exc.error_class, str(exc)) from exc
        except TTSJobError:
            raise
        except LeaseLost:
            raise
        except Exception as exc:
            raise TTSJobError("materialise", getattr(exc, "error_class", "extract_failed"), str(exc),
                              disposition=getattr(exc, "disposition", ""),
                              reason_code=getattr(exc, "reason_code", "")) from exc
        usage: dict[str, Any] | None = None
        hangups: list[str] = []
        presentation: dict[str, Any] = {}
        _cancel()
        stage = "compile"
        try:
            artifact = work / "artifact.md"
            primary_document: Path | None = None
            enrichment_result = None
            if x_mode:
                ex = compile_explained_artifact_pi(
                    source_md, meta, artifact, input_json=input_json,
                    work_dir=work, cancel=_cancel)
                script_path = work / "script.md"
                script_path.write_text(ex.script_markdown, encoding="utf-8")
                title = ex.title
                usage = ex.usage
                hangups = ex.hangups
                speech_text = artifact_to_speech_text(artifact)
                description = str(ex.model_output.get("source_summary") or title)
            else:
                title = resolve_title(
                    source_md,
                    meta,
                    settings=settings,
                    title_hint=input_json.get("titleHint"),
                    http_post=openai_post,
                )
                cleaned = clean_faithful_markdown(
                    source_md.read_text(encoding="utf-8"), episode_title=title
                )
                cleaned_path = work / "faithful-clean.md"
                cleaned_path.write_text(cleaned.markdown, encoding="utf-8")
                cleaned_meta = dict(meta)
                cleaned_meta["source_markdown_sha256"] = meta.get("markdown_sha256", "")
                cleaned_meta["markdown_sha256"] = hashlib.sha256(cleaned.markdown.encode("utf-8")).hexdigest()
                cleaned_meta["presentation_cleanup"] = cleaned.diagnostics
                cleaned_meta_path = work / "faithful-clean.meta.json"
                cleaned_meta_path.write_text(json.dumps(cleaned_meta, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
                classified = classify_input([acquired.source_ref])
                artifact = compile_artifact(
                    [{"markdown_path": cleaned_path, "meta_path": cleaned_meta_path}],
                    str(classified.get("input_class") or "single_piece"),
                    policy={"faithfulCleanupVersion": CLEANUP_VERSION},
                    out_path=artifact,
                )
                speech_text = artifact_to_speech_text(artifact)
                description = episode_description(title, cleaned.topics)
                presentation = {
                    "title": title,
                    "description": description,
                    "topics": cleaned.topics,
                    "sectionHeadings": cleaned.section_headings,
                    "cleanup": cleaned.diagnostics,
                }
            speech_path = work / "speech.txt"
            speech_path.write_text(speech_text, encoding="utf-8")
            if not speech_text.strip():
                raise TTSJobError("compile", "input_empty", "speech text is empty")
            names = build_episode_names(EpisodeNamingMetadata(
                title=title,
                identity=fp,
                creators=_metadata_creators(meta),
                channel=str(meta.get("channel") or meta.get("publisher") or ""),
                series=str(meta.get("series") or ""),
                published_at=(str(meta.get("published_at"))
                              if meta.get("published_at") else None),
            ))
            # A retry must render the prefix publication will actually use.
            # build_episode_names derives it from an LLM title and today's
            # date, so a second attempt can compute a different prefix while
            # the publisher still uploads under the slug pinned by the first
            # attempt -- leaving every in-document URL pointing at keys that
            # were never written. Ask the publisher for the pinned slug first.
            pinned_prefix = pinned_artifact_prefix(work, fp)
            if pinned_prefix and pinned_prefix != names.prefix:
                logger.warning(
                    "reusing artifact prefix pinned by an earlier attempt: "
                    "%s (recomputed %s)", pinned_prefix, names.prefix)
                names = names_from_prefix(pinned_prefix)
        except ExplainError as exc:
            raise TTSJobError("model", exc.error_class, str(exc)) from exc
        except TTSJobError:
            raise
        except LeaseLost:
            raise
        except Exception as exc:
            raise TTSJobError("compile", "compile_failed", type(exc).__name__[:80]) from exc

        # Run the script-only analysis before speech-provider spend. The second
        # call after synthesis reuses these checkpoints and only re-renders
        # notes with final duration and URLs.
        _cancel()
        stage = "enrichment"
        try:
            if x_mode:
                try:
                    document_source = ScriptSource(
                        ex.script_markdown,
                        source_summary=str(ex.model_output.get("source_summary") or ""),
                        speech_markdown=speech_text)
                except ValueError as exc:
                    # Degraded: the notes lose plan-derived topics and summary
                    # provenance. Enrichment is required, so this must be
                    # visible; otherwise a downgraded episode is
                    # indistinguishable from a clean one in the notification
                    # and the review flags.
                    logger.warning(
                        "x-tts structured script failed its enrichment "
                        "contract; falling back to plain script: %s", exc)
                    hangups = list(hangups) + [
                        f"enrichment_degraded: structured script rejected ({exc})"]
                    document_source = PlainScriptSource(
                        speech_text, content_origin="x_tts_speech_script")
            else:
                document_source = PlainScriptSource(
                    speech_text, content_origin="faithful_tts_speech_script")
            public_root = f"{settings.public_base_url}/tts/episodes/{fp}"
            enrich(
                source=document_source, work_dir=work / "enrichment", title=title,
                slug=names.prefix, published=None, duration_seconds=None,
                mp3_url=f"{public_root}/{names.audio_name}",
                notes_url=f"{public_root}/{names.notes_name}",
                script_url=f"{public_root}/{names.script_name}",
                source_url=str(input_json.get("sourceUrl") or acquired.source_ref),
                runner_factory=(lambda role: pi_runner_factory(role)) if pi_runner_factory else None,
            )
        except LeaseLost:
            raise
        except Exception as exc:
            raise TTSJobError("enrichment", getattr(exc, "error_class", "enrichment_failed"), type(exc).__name__[:80]) from exc

        _cancel()
        stage = "speech"
        try:
            azure = lambda v: AzureSpeechProvider(base_url=settings.azure_tts_base_url, api_key=settings.azure_tts_api_key, key_cmd=settings.azure_tts_key_cmd, model=settings.azure_tts_model, voice=v, http_post=gemini_post)
            if engine == "self-hosted":
                # Azure reads the whole episode again if m3 fails: one episode, one voice.
                providers = [SelfHostedSpeechProvider(base_url=settings.self_hosted_speech_url, token=settings.self_hosted_speech_token, model=settings.self_hosted_speech_model, voice=voice, http_post=gemini_post),
                             azure(settings.fallback_voice)]
            elif engine == "azure":
                providers = [azure(voice)]
            else:
                providers = [GeminiSpeechProvider(api_key=settings.gemini_api_key, voice=voice, http_post=gemini_post)]
            speech, provider, fallback_notes = synthesize_with_fallback(
                speech_text,
                work / "speech",
                providers=providers,
                ffmpeg_bin=settings.ffmpeg_bin,
                ffprobe_bin=settings.ffprobe_bin,
                runner=media_runner,
                cancel=_cancel,
            )
            for note in fallback_notes:
                logger.warning("tts speech fell back: %s", note)
            hangups = list(hangups) + fallback_notes
        except SpeechError as exc:
            raise TTSJobError("speech", exc.error_class, str(exc)) from exc
        _cancel()
        stage = "enrichment"
        try:
            enrichment_result = enrich(
                source=document_source, work_dir=work / "enrichment", title=title,
                slug=names.prefix, published=None,
                duration_seconds=speech.duration_seconds,
                mp3_url=f"{public_root}/{names.audio_name}",
                notes_url=f"{public_root}/{names.notes_name}",
                script_url=f"{public_root}/{names.script_name}",
                source_url=str(input_json.get("sourceUrl") or acquired.source_ref),
                runner_factory=(lambda role: pi_runner_factory(role)) if pi_runner_factory else None,
            )
            script_document = work / names.script_name
            notes_document = work / names.notes_name
            script_document.write_text(enrichment_result.script_markdown, encoding="utf-8")
            notes_document.write_text(enrichment_result.notes_markdown, encoding="utf-8")
            primary_document = notes_document if x_mode else None
            description = enrichment_result.description
            enrichment_meta = {
                "enrichment": enrichment_result.analysis,
                "enrichmentManifest": json.loads(enrichment_result.manifest),
                "enrichmentProvenance": {
                    "contentOrigin": getattr(document_source, "content_origin", "unknown"),
                    "topicOrigin": getattr(document_source, "topic_origin", "unknown"),
                    "summaryOrigin": getattr(document_source, "summary_origin", "unknown"),
                    "scriptSha256": hashlib.sha256(speech_text.encode("utf-8")).hexdigest(),
                },
            }
            presentation = ({"reader": ex.process_meta, **enrichment_meta}
                            if x_mode else {**presentation, **enrichment_meta})
        except LeaseLost:
            raise
        except Exception as exc:
            raise TTSJobError("enrichment", getattr(exc, "error_class", "enrichment_failed"), type(exc).__name__[:80]) from exc
        _cancel()
        stage = "manifest"
        manifest_path = work / "manifest.json"
        try:
            total_seconds_before_publish = round(time.time() - started, 3)
        except Exception as exc:
            raise TTSJobError("manifest", "manifest_failed", str(exc)) from exc
        try:
            manifest_artifacts = {"sourceOriginal": acquired.path, "sourceMarkdown": source_md,
                                 "artifactMarkdown": artifact, "speechText": speech_path,
                                 "publicScript": script_document,
                                 "publicNotes": notes_document,
                                 "audioMp3": speech.audio_path}
            if primary_document is not None:
                manifest_artifacts["primaryDocument"] = primary_document
            write_manifest(manifest_path, source={"sourceKey": source_key, "sourceHash": acquired.source_hash, "sourceRef": acquired.source_ref}, artifacts=manifest_artifacts, chunk_receipts=speech.receipts, model_usage=usage, voice=provider.voice, timings={"totalSecondsBeforePublish": total_seconds_before_publish, "audioDurationSeconds": speech.duration_seconds}, warnings=list(meta.get("warnings") or []) + hangups, presentation=presentation)
        except Exception as exc:
            raise TTSJobError("manifest", "manifest_failed", str(exc)) from exc
        stage = "checkpoint"
        try:
            written_checkpoint = write_generation_checkpoint(
                work,
                identity=fp,
                attempt_id=attempt_id,
                source={
                    "hash": acquired.source_hash,
                    "ref": acquired.source_ref,
                    "ext": acquired.ext,
                },
                publication={
                    "title": title,
                    "description": description,
                    "route": "x-tts" if x_mode else "tts",
                    "episodeKind": "explained_read" if x_mode else "faithful_read",
                    "durationSeconds": speech.duration_seconds,
                    "xMode": x_mode,
                    "modelUsage": usage or {},
                    "hangups": hangups,
                },
                artifacts={
                    "sourceOriginal": acquired.path,
                    "sourceMarkdown": source_md,
                    "sourceMeta": source_meta,
                    "artifactMarkdown": artifact,
                    "speechText": speech_path,
                    "publicScript": script_document,
                    "publicNotes": notes_document,
                    "audioMp3": speech.audio_path,
                    "manifestJson": manifest_path,
                    **({"primaryDocument": primary_document} if primary_document is not None else {}),
                },
            )
        except GenerationCheckpointIdentityConflict as exc:
            raise TTSJobError("checkpoint", "publish_identity_conflict", str(exc)) from exc
        except GenerationCheckpointInvalid as exc:
            raise TTSJobError("checkpoint", "generation_checkpoint_invalid", str(exc)) from exc
        except OSError as exc:
            raise TTSJobError("checkpoint", "generation_checkpoint_write_failed", str(exc)) from exc
        _cancel()
        stage = "publish"
        try:
            ep = publish_episode(
                fingerprint=fp, title=title, description=description,
                route="x-tts" if x_mode else "tts",
                episode_kind="explained_read" if x_mode else "faithful_read",
                source_hash=acquired.source_hash, source_original=acquired.path,
                source_ext=acquired.ext, source_md=source_md, source_meta=source_meta,
                artifact_md=artifact, speech_txt=speech_path,
                script_document=script_document, notes_document=notes_document,
                artifact_prefix=names.prefix,
                audio_mp3=speech.audio_path, manifest_json=manifest_path,
                duration_seconds=speech.duration_seconds,
                primary_document=primary_document, settings=settings,
                runner=rclone_runner, http_post=episode_post, http_get=episode_get,
                state_dir=work, cancel=_cancel)
        except PublishError as exc:
            raise TTSJobError("publish", exc.error_class, str(exc)) from exc
    except (TTSJobError, LeaseLost):
        raise
    except Exception as exc:
        raise TTSJobError(stage, "unexpected_failure", type(exc).__name__[:80]) from exc
    return result_from_checkpoint(written_checkpoint, ep, source_key=source_key)


async def process_claim(*, client: Any, settings: Settings, claim: dict[str, Any], hooks: dict[str, Any] | None = None) -> None:
    hooks = hooks or {}
    job = dict(claim["job"])
    attempt = dict(claim["attempt"])
    input_json = dict(job.get("inputJson") or {})
    input_json.setdefault("workloadType", job.get("workloadType"))
    job_id = str(job["jobId"])
    input_json["sourceKey"] = str(input_json.get("sourceKey") or job_id)
    attempt_id = str(attempt["attemptId"])
    lease_token = str(attempt["leaseToken"])
    await asyncio.to_thread(client.mark_running, job_id=job_id, attempt_id=attempt_id, lease_token=lease_token)
    heartbeat = hooks.pop("heartbeat", None) or AttemptHeartbeat(
        client=client, job_id=job_id, attempt_id=attempt_id,
        lease_token=lease_token, lease_seconds=settings.lease_seconds)
    heartbeat.start()
    enabled = os.environ.get("TG_TOOLS_PROGRESS_ENABLED") == "1"
    reporter = ProgressReporter(
        send=client.append_progress, job_id=job_id, attempt_id=attempt_id,
        lease_token=lease_token, check=heartbeat.check,
    ).start() if enabled else NullProgress()
    reporter.check = heartbeat.check
    try:
        try:
            with use_progress(reporter):
                result = await asyncio.to_thread(
                    execute_job, settings, input_json, attempt_id=attempt_id, cancel_check=heartbeat.check, **hooks)
        except LeaseLost:
            # Known lease loss: start no new external effect, do not finalize
            # with a stale token, discard late output. The runtime re-leases.
            logger.warning("abandoning attempt after lease loss job=%s", job_id)
            return
        except Exception as exc:
            if not isinstance(exc, TTSJobError):
                exc = TTSJobError("worker", "unexpected_failure", type(exc).__name__[:80])
            if heartbeat.lost:
                logger.warning("abandoning failed attempt after lease loss job=%s", job_id)
                return
            if enabled:
                await asyncio.to_thread(reporter.flush)
            await _finalize_failure(client=client, exc=exc, job_id=job_id, attempt_id=attempt_id, lease_token=lease_token, lease_check=heartbeat.check)
            return
        if heartbeat.lost:
            logger.warning("discarding completed work after lease loss job=%s", job_id)
            return
        if enabled:
            await asyncio.to_thread(reporter.flush)
        # Renewal stays active THROUGH the finalize response;
        # fence immediately before the call.
        heartbeat.check()
        await asyncio.to_thread(client.finalize, {"jobId": job_id, "attemptId": attempt_id, "leaseToken": lease_token, "outcome": "succeeded", "resultJson": result})
    except LeaseLost:
        logger.warning("lease lost immediately before finalize job=%s", job_id)
        return
    finally:
        if enabled:
            await asyncio.to_thread(reporter.close)
        await asyncio.to_thread(heartbeat.stop)


async def _finalize_failure(*, client: Any, exc: TTSJobError, job_id: str, attempt_id: str, lease_token: str, lease_check: Any = None) -> None:
    policy = {"reason_code": exc.reason_code, "recovery_available": exc.recovery_available}
    error_class = effective_error_class(exc.error_class, **policy)
    outcome, backoff_sec = classify_outcome(exc.error_class, **policy)
    result = {"ok": False, "schemaVersion": 1, "stage": exc.stage,
              "errorClass": error_class, "message": str(exc), "outcome": outcome}
    if exc.disposition or exc.reason_code:
        result.update(originalErrorClass=exc.error_class, disposition=exc.disposition,
                      reasonCode=exc.reason_code, recoveryAvailable=exc.recovery_available)
    finalize_req = {"jobId": job_id, "attemptId": attempt_id, "leaseToken": lease_token,
                    "outcome": outcome, "errorClass": error_class,
                    "errorText": str(exc), "resultJson": result}
    if outcome == "retryable_error":
        finalize_req["backoffSec"] = backoff_sec
    if lease_check is not None:
        lease_check()  # fence immediately before the failure finalize too
    await asyncio.to_thread(client.finalize, finalize_req)


async def _membership_heartbeat_loop(
    client: Any, worker_id: str, stop: asyncio.Event, *, interval_sec: float = 30,
) -> None:
    """Keep worker membership fresh while a long job runs in a worker thread."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(
                client.heartbeat_worker,
                worker_id=worker_id,
                metadata_json={"phase": "available-or-busy"},
            )
        except RuntimeClientError:
            logger.exception("Runtime membership heartbeat failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, interval_sec))
        except asyncio.TimeoutError:
            pass


async def worker_loop(settings: Settings) -> None:
    from .startup import membership_heartbeat_interval, validate_startup

    fatal, warnings = validate_startup(settings)
    for problem in warnings:
        logger.warning("startup validation: %s", problem)
    if fatal:
        for problem in fatal:
            logger.error("startup validation: %s", problem)
        # Abort BEFORE registering: an unhealthy worker must not claim jobs
        # it cannot complete and generate avoidable retries/dead jobs.
        raise SystemExit("tts-worker startup validation failed; refusing to register")
    try:
        ensure_work_state(settings.work_dir)
    except WorkRootError as exc:
        raise SystemExit(f"tts-worker work state unavailable: {exc}") from exc
    client = RuntimeClient(settings.runtime_url, settings.runtime_bearer_token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await asyncio.to_thread(client.register_worker, worker_id=settings.worker_id, worker_venue=settings.worker_venue, capacity=settings.capacity, supported_execution_profiles=settings.supported_profiles, workload_types=settings.workload_types, producer_systems=("tts", "x-tts"), allowed_queue_classes=("foreground",), queue_preference=("foreground",), metadata_json={"component": "tts-worker", "pid": os.getpid()})
    membership_task = asyncio.create_task(_membership_heartbeat_loop(
        client,
        settings.worker_id,
        stop,
        interval_sec=membership_heartbeat_interval(),
    ))
    try:
        while not stop.is_set():
            await asyncio.to_thread(client.heartbeat_worker, worker_id=settings.worker_id, metadata_json={"phase": "idle"})
            claim = await asyncio.to_thread(client.claim_job, worker_id=settings.worker_id, lease_duration_sec=settings.lease_seconds, supported_execution_profiles=settings.supported_profiles, workload_types=settings.workload_types, worker_venue=settings.worker_venue, capacity=settings.capacity)
            if not claim:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await process_claim(client=client, settings=settings, claim=claim)
            except RuntimeClientError:
                logger.exception("Runtime API failed while processing TTS claim")
            except Exception:
                logger.exception("Unexpected TTS worker failure")
    finally:
        stop.set()
        await membership_task


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(worker_loop(load_settings()))
