"""Role-to-provider configuration for the shared Pi harness."""
from __future__ import annotations

import hashlib
import json
import os

# These prompts are part of the reader identity. Keep their bytes stable.
_JSON_ROLE_PROMPT = (
    "You analyse a source document and reply with a single JSON object and "
    "nothing else. No preamble, no commentary, no code fences."
)
# Model tiers. Roles name a tier, so one edit (or one "models" override)
# moves every role on that tier. Provider "claude" runs `claude -p` on the
# host's subscription login; every other provider runs `pi -p`.
_MODELS = {
    "sol": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    "luna": {"provider": "openai-codex", "model": "gpt-5.6-luna"},
    "opus": {"provider": "claude", "model": "claude-opus-5-5"},
    "haiku": {"provider": "claude", "model": "claude-haiku-5-5"},
    "sonnet": {"provider": "claude", "model": "claude-sonnet-5-5"},
}

# Reader v4: one writer thread (it also judges the review and revises) and
# one second reader. Switch a model with TTS_PI_ROLES_FILE, e.g.
#   {"write": {"tier": "sol", "thinking": "high"}}
#   {"review": {"provider": "openai-codex", "model": "gpt-6.1-sol"}}
# "thinking" is passed as claude --effort or pi --thinking.
_READER_DEFAULTS = {
    "write": {"tier": "opus", "thinking": "high", "flags": [], "system_prompt": "You write spoken explanations of written sources for a single listener."},
    "review": {"tier": "haiku", "thinking": "medium", "flags": [], "system_prompt": "You check a spoken script against its source for a listener's sake."},
}
READER_ROLES = tuple(_READER_DEFAULTS)

# The two transcript roles have no tier of their own: the enrichment tier file names it per
# pipeline (see enrichment_tier). The roles file may only change effort and prompt for them.
_TRANSCRIPT_DEFAULTS = {
    "transcript_topics": {"thinking": "max", "flags": [], "system_prompt": _JSON_ROLE_PROMPT},
    "transcript_mentions": {"thinking": "high", "flags": [], "system_prompt": _JSON_ROLE_PROMPT},
}
TRANSCRIPT_ROLES = tuple(_TRANSCRIPT_DEFAULTS)
_MODEL_KEYS = ("tier", "provider", "model")
# Providers no enrichment tier may resolve to: the Codex login is gone (X-60).
REFUSED_ENRICHMENT_PROVIDERS = ("openai-codex",)
ENRICHMENT_FILE_ENV = "TG_TOOLS_ENRICHMENT_FILE"
DEFAULT_ENRICHMENT_FILE = "~/.config/tg-tools/enrichment.json"

_DEFAULTS = {**_READER_DEFAULTS, **_TRANSCRIPT_DEFAULTS}
_KEYS = ("provider", "model", "thinking", "flags", "system_prompt")


class EnrichmentConfigError(ValueError):
    """Enrichment model configuration is missing or wrong. A job fails with this ``error_class``."""
    error_class = "enrichment_config"


def _roles(transcript_tier: str | None = None) -> dict:
    """Apply TTS_PI_ROLES_FILE, then resolve each role's tier.

    The file may hold a "models" object overriding tiers by name, plus
    per-role objects overriding role fields (including "tier", or an
    explicit "provider"/"model" that wins over the tier). The transcript
    roles take their tier from ``transcript_tier`` only; without one they are
    left out, and naming a tier/provider/model for them here is an error.
    """
    models = {name: dict(config) for name, config in _MODELS.items()}
    roles = {name: dict(config) for name, config in _DEFAULTS.items()}
    path = os.environ.get("TTS_PI_ROLES_FILE")
    if path:
        with open(path, encoding="utf-8") as source:
            overrides = json.load(source)
        if not isinstance(overrides, dict):
            raise ValueError("role override must be an object")
        tiers = overrides.pop("models", {})
        if not isinstance(tiers, dict):
            raise ValueError("models override must be an object")
        if transcript_tier in tiers:
            raise EnrichmentConfigError(
                "%s redefines model tier %r in its \"models\"; the enrichment tier file (%s) picks that tier, "
                "so its meaning must live in one place (remove it from the roles file)"
                % (path, transcript_tier, ENRICHMENT_FILE_ENV))
        for name, override in tiers.items():
            if not isinstance(override, dict):
                raise ValueError("model tier override must be an object")
            models.setdefault(name, {}).update(override)
        for name, override in overrides.items():
            if name not in roles:
                raise ValueError("unknown role override: %s" % name)
            if not isinstance(override, dict):
                raise ValueError("role override must be an object")
            if name in TRANSCRIPT_ROLES and (named := [key for key in _MODEL_KEYS if key in override]):
                raise EnrichmentConfigError(
                    "role %s sets %s in %s; the model of a transcript role comes from the enrichment "
                    "tier file (%s)" % (name, "/".join(named), path, ENRICHMENT_FILE_ENV))
            roles[name].update(override)
    for name in TRANSCRIPT_ROLES:
        if transcript_tier is None:
            del roles[name]
        else:
            roles[name]["tier"] = transcript_tier
    for name, config in roles.items():
        tier = config.pop("tier", None)
        if tier is not None:
            if tier not in models:
                error = EnrichmentConfigError if name in TRANSCRIPT_ROLES else ValueError
                raise error("role %s uses unknown model tier: %s" % (name, tier))
            config.update({key: value for key, value in models[tier].items()
                           if key not in config})
        if not config.get("provider") or not config.get("model"):
            raise ValueError("role %s has no provider/model" % name)
    return roles


def enrichment_file() -> str:
    return os.path.expanduser(os.environ.get(ENRICHMENT_FILE_ENV) or DEFAULT_ENRICHMENT_FILE)


def enrichment_tier(pipeline: str) -> str:
    """The model tier the enrichment of ``pipeline`` runs on, read from the tier file now.

    Call once at the start of a job, before any download or paid call, and pass the result on:
    editing the file takes effect on the next job with no restart. Everything wrong is an
    ``EnrichmentConfigError`` (error_class ``enrichment_config``): no file, bad JSON, a pipeline
    the file does not list, an unknown tier, a tier on a refused provider (openai-codex), or a roles
    file that names a model for a transcript role or redefines the chosen tier in its "models". There is no fallback tier.
    """
    path = enrichment_file()
    try:
        with open(path, encoding="utf-8") as source:
            tiers = json.load(source)
    except (OSError, ValueError) as exc:
        raise EnrichmentConfigError("cannot read the enrichment tier file %s: %s" % (path, exc)) from exc
    if not isinstance(tiers, dict):
        raise EnrichmentConfigError("%s must be a JSON object of pipeline -> tier" % path)
    if pipeline not in tiers:
        raise EnrichmentConfigError("%s has no tier for pipeline %r (it lists: %s)"
                                    % (path, pipeline, ", ".join(sorted(tiers)) or "none"))
    tier = tiers[pipeline]
    if not isinstance(tier, str) or not tier:
        raise EnrichmentConfigError("%s: the tier of pipeline %r must be a name" % (path, pipeline))
    try:
        for role in TRANSCRIPT_ROLES:
            provider = resolve_role(role, tier=tier)["provider"]
            if provider in REFUSED_ENRICHMENT_PROVIDERS:
                raise EnrichmentConfigError(
                    "pipeline %r, tier %r: role %s would run on provider %s, which is not available (X-60); "
                    "pick a claude tier in %s" % (pipeline, tier, role, provider, path))
    except EnrichmentConfigError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise EnrichmentConfigError("pipeline %r, tier %r: %s" % (pipeline, tier, exc)) from exc
    return tier


def resolve_role(role: str, tier: str | None = None) -> dict:
    """Resolve one known role, applying only bounded field overrides.

    A transcript role needs ``tier`` (from ``enrichment_tier``); it has no default.
    """
    if role in TRANSCRIPT_ROLES and not tier:
        raise EnrichmentConfigError("role %s needs an enrichment tier (see enrichment_tier)" % role)
    roles = _roles(tier)
    if role not in roles:
        raise KeyError("unknown role %r; known roles: %s" %
                       (role, ", ".join(sorted(roles))))
    return {key: roles[role].get(key) for key in _KEYS}


def reader_role_configs() -> dict[str, dict]:
    """Return the applied reader role configurations."""
    roles = _roles()
    return {name: {key: roles[name].get(key) for key in _KEYS}
            for name in READER_ROLES}


def reader_role_map_hash(configs: dict[str, dict] | None = None) -> str:
    configs = configs or reader_role_configs()
    canonical = {}
    for name in READER_ROLES:
        cfg = configs[name]
        canonical[name] = {
            "provider": cfg.get("provider"), "model": cfg.get("model"),
            "thinking": cfg.get("thinking"), "flags": cfg.get("flags"),
            "systemPromptSha256": hashlib.sha256(
                str(cfg.get("system_prompt") or "").encode("utf-8")).hexdigest(),
        }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True,
                                     separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()
