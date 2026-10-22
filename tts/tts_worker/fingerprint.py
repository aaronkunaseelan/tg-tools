"""Normalized execution fingerprints for faithful and explained reads."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from episode_enrichment.prompts import READER_MENTIONS_PROMPT
from tts_reader.v4 import PROMPT_VERSION as PROMPT_VERSION_EXPLAIN

MATERIALISER_VERSION = "materialise_v4_tg_documents_v1"
PROMPT_VERSION_FAITHFUL = "tts_faithful_v2_clean_presentation"
READER_PIPELINE_VERSION = "reader_v4_write_review_revise"
SPEECH_RENDERER_VERSION = "speech_renderer_v3_clean_sections"
EXPLAIN_SPEECH_RENDERER_VERSION = "explain_speech_renderer_v1_heading_free"
REQUIRED_ENRICHMENT_CONTRACT_VERSION = "episode_enrichment_script_source_v1"
ENRICHMENT_RENDERER_VERSION = "episode_enrichment_script_notes_v2"
EPISODE_NAMING_VERSION = "metadata_proper_nouns_v1"
PUBLIC_DOCUMENTS_VERSION = "script_notes_manifest_v2"
READER_MENTION_PROMPT_VERSION = hashlib.sha256(
    READER_MENTIONS_PROMPT.encode("utf-8")).hexdigest()
MANIFEST_SCHEMA_VERSION = 2


class RoleMapError(RuntimeError):
    """The mandatory reader role map is absent or not pinned."""


def reader_role_map() -> dict[str, dict[str, Any]]:
    """Return the resolved reader roles with their identity hash.

    System prompts stay pinned: they are part of the reader identity.
    Provider, model and thinking come from the tier/role config and are
    covered by ``_hash``, so a model switch is a new execution identity.
    """
    from pi_harness.roles import reader_role_configs, reader_role_map_hash
    try:
        configs = reader_role_configs()
        expected = {
            "write": "e9119b137d5a904b4b471b09defbcbe23b1cdf05d43766cb8052f5d0b3741a66",
            "review": "d89c03ff8a10936047b58507ae7a3ec5c633f09df78fac01e4cd248b9242e555",
        }
        if set(configs) != set(expected):
            raise RoleMapError("reader role map must contain exactly the pinned roles")
        result = {}
        for name, digest in expected.items():
            cfg = configs[name]
            actual_digest = hashlib.sha256(
                str(cfg.get("system_prompt") or "").encode("utf-8")).hexdigest()
            if actual_digest != digest:
                raise RoleMapError("role %s system prompt is not the pinned prompt" % name)
            result[name] = {
                "provider": cfg.get("provider"), "model": cfg.get("model"),
                "thinking": cfg.get("thinking"),
                "flags": list(cfg.get("flags") or []),
                "systemPromptSha256": digest,
            }
        result["_hash"] = reader_role_map_hash(configs)
        return result
    except RoleMapError:
        raise
    except (OSError, TypeError, AttributeError, ValueError, KeyError) as exc:
        raise RoleMapError(str(exc)) from exc


def execution_fingerprint(
    *, source_hash: str, mode: str, requested_length: str, voice: str,
    speech_provider: str, speech_model: str,
    reader_roles: dict[str, Any] | None = None,
    transform_contract_config_version: str = "tg-documents-v1",
) -> str:
    """Hash every output-determining input; length is inert only for explain."""
    identity: dict[str, Any] = {
        "sourceHash": source_hash, "mode": mode, "voice": voice.lower(),
        "speechProvider": speech_provider, "speechModel": speech_model,
        "materialiserVersion": MATERIALISER_VERSION,
        "transformContractConfigVersion": transform_contract_config_version,
        "promptVersion": PROMPT_VERSION_EXPLAIN if mode == "explain" else PROMPT_VERSION_FAITHFUL,
        "speechRendererVersion": (EXPLAIN_SPEECH_RENDERER_VERSION if mode == "explain"
                                   else SPEECH_RENDERER_VERSION),
        "manifestSchemaVersion": MANIFEST_SCHEMA_VERSION,
        "episodeNamingVersion": EPISODE_NAMING_VERSION,
        "publicDocumentsVersion": PUBLIC_DOCUMENTS_VERSION,
    }
    if mode == "read":
        # Keep the faithful-read golden identity byte-for-byte stable.
        identity.update({"requestedLength": requested_length,
                         "readerRoles": dict(sorted((reader_roles or {}).items())),
                         "readerPipelineVersion": None})
    else:
        identity.update({
            "readerRoles": reader_roles or {},
            "readerPipelineVersion": READER_PIPELINE_VERSION,
            "requiredEnrichmentContractVersion": REQUIRED_ENRICHMENT_CONTRACT_VERSION,
            "readerMentionPromptVersion": READER_MENTION_PROMPT_VERSION,
            "enrichmentRendererVersion": ENRICHMENT_RENDERER_VERSION,
        })
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def payload_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
