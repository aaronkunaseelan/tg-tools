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

_TRANSCRIPT_DEFAULTS = {
    "transcript_topics": {"tier": "luna", "thinking": "max", "flags": [], "system_prompt": _JSON_ROLE_PROMPT},
    "transcript_mentions": {"tier": "luna", "thinking": "high", "flags": [], "system_prompt": _JSON_ROLE_PROMPT},
}

_DEFAULTS = {**_READER_DEFAULTS, **_TRANSCRIPT_DEFAULTS}
_KEYS = ("provider", "model", "thinking", "flags", "system_prompt")


def _roles() -> dict:
    """Apply TTS_PI_ROLES_FILE, then resolve each role's tier.

    The file may hold a "models" object overriding tiers by name, plus
    per-role objects overriding role fields (including "tier", or an
    explicit "provider"/"model" that wins over the tier).
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
        for name, override in tiers.items():
            if not isinstance(override, dict):
                raise ValueError("model tier override must be an object")
            models.setdefault(name, {}).update(override)
        for name, override in overrides.items():
            if name not in roles:
                raise ValueError("unknown role override: %s" % name)
            if not isinstance(override, dict):
                raise ValueError("role override must be an object")
            roles[name].update(override)
    for name, config in roles.items():
        tier = config.pop("tier", None)
        if tier is not None:
            if tier not in models:
                raise ValueError("role %s uses unknown model tier: %s" % (name, tier))
            config.update({key: value for key, value in models[tier].items()
                           if key not in config})
        if not config.get("provider") or not config.get("model"):
            raise ValueError("role %s has no provider/model" % name)
    return roles


def resolve_role(role: str) -> dict:
    """Resolve one known role, applying only bounded field overrides."""
    roles = _roles()
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
