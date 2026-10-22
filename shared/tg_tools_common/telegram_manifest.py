"""Stdlib JSON service-manifest loader for Telegram route registries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence, get_args

from tg_tools_common.telegram_routes import (
    RouteDefinition,
    RouteKind,
    RouteRule,
    TelegramRouteRegistry,
    TextMode,
    ThreadMode,
)


VALID_ROUTE_KINDS = frozenset(get_args(RouteKind))
VALID_THREAD_MODES = frozenset(get_args(ThreadMode))
VALID_TEXT_MODES = frozenset(get_args(TextMode))
SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"


@dataclass(frozen=True)
class ManifestRule:
    rule_id: str
    rule_version: str = "v1"
    command_names: frozenset[str] = field(default_factory=frozenset)
    media_kinds: frozenset[str] = field(default_factory=frozenset)
    text_modes: frozenset[TextMode] = field(default_factory=frozenset)
    require_reply: bool | None = None
    allowed_sender_user_ids: frozenset[int] = field(default_factory=frozenset)

    def to_route_rule(self) -> RouteRule:
        return RouteRule(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            command_names=self.command_names,
            media_kinds=self.media_kinds,
            text_modes=self.text_modes,
            require_reply=self.require_reply,
            allowed_sender_user_ids=self.allowed_sender_user_ids,
        )


@dataclass(frozen=True)
class ManifestRoute:
    route_id: str
    route_kind: RouteKind
    workload_type: str
    owner_system: str
    bot_identity: str
    chat_id_binding: str
    thread_mode: ThreadMode = "exact"
    thread_id_binding: str | None = None
    rules: tuple[ManifestRule, ...] = field(default_factory=tuple)
    enabled: bool = True

    def to_route_definition(self, bindings: Mapping[str, int | None]) -> RouteDefinition | None:
        chat_id = _binding_value(bindings, self.chat_id_binding, self.route_id)
        if chat_id is None:
            return None
        thread_id = None
        if self.thread_id_binding is not None:
            thread_id = _binding_value(bindings, self.thread_id_binding, self.route_id)
            if thread_id is None:
                return None
        elif self.thread_mode == "exact":
            raise ValueError(f"{self.route_id}: exact thread mode requires threadIdBinding")

        return RouteDefinition(
            route_id=self.route_id,
            route_kind=self.route_kind,
            workload_type=self.workload_type,
            owner_system=self.owner_system,
            bot_identity=self.bot_identity,
            chat_id=chat_id,
            thread_mode=self.thread_mode,
            thread_id=thread_id,
            rules=tuple(rule.to_route_rule() for rule in self.rules),
            enabled=self.enabled,
        )


@dataclass(frozen=True)
class ServiceManifest:
    service: str
    bot_identity: str
    owner_system: str
    routes: tuple[ManifestRoute, ...]


def load_service_manifest(path: str | Path) -> ServiceManifest:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: manifest root must be an object")
    return parse_service_manifest(raw, source=str(path))


def load_named_service_manifest(name: str) -> ServiceManifest:
    return load_service_manifest(SERVICES_DIR / f"{name}.json")


def manifests_for_identity(bot_identity: str) -> tuple[ServiceManifest, ...]:
    """Every service manifest whose routes this bot identity serves."""
    manifests = tuple(load_service_manifest(path) for path in sorted(SERVICES_DIR.glob("*.json")))
    return tuple(manifest for manifest in manifests if manifest.bot_identity == bot_identity)


def parse_service_manifest(raw: Mapping[str, Any], *, source: str = "<manifest>") -> ServiceManifest:
    service = _required_str(raw, "service", source)
    bot_identity = _required_str(raw, "botIdentity", source)
    owner_system = _required_str(raw, "ownerSystem", source)
    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, list) or not routes_raw:
        raise ValueError(f"{source}: routes must be a non-empty list")

    routes = tuple(
        _parse_route(
            route_raw,
            source=f"{source}:routes[{index}]",
            manifest_bot_identity=bot_identity,
            manifest_owner_system=owner_system,
        )
        for index, route_raw in enumerate(routes_raw)
    )
    return ServiceManifest(
        service=service,
        bot_identity=bot_identity,
        owner_system=owner_system,
        routes=routes,
    )


def build_registry_from_manifests(
    manifests: Sequence[ServiceManifest],
    *,
    bindings: Mapping[str, int | None],
) -> TelegramRouteRegistry:
    routes: list[RouteDefinition] = []
    for manifest in manifests:
        for route in manifest.routes:
            route_definition = route.to_route_definition(bindings)
            if route_definition is not None:
                routes.append(route_definition)
    return TelegramRouteRegistry(routes)


def _parse_route(
    raw: Any,
    *,
    source: str,
    manifest_bot_identity: str,
    manifest_owner_system: str,
) -> ManifestRoute:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: route must be an object")

    route_id = _required_str(raw, "routeId", source)
    route_kind = _required_enum(raw, "routeKind", VALID_ROUTE_KINDS, source)
    workload_type = _required_str(raw, "workloadType", source)
    chat_id_binding = _required_str(raw, "chatIdBinding", source)
    owner_system = _optional_str(raw, "ownerSystem") or manifest_owner_system
    bot_identity = _optional_str(raw, "botIdentity") or manifest_bot_identity
    thread_mode = _optional_enum(raw, "threadMode", VALID_THREAD_MODES, source) or "exact"
    thread_id_binding = _optional_str(raw, "threadIdBinding")
    enabled = bool(raw.get("enabled", True))

    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ValueError(f"{source}: rules must be a list")
    rules = tuple(
        _parse_rule(rule_raw, source=f"{source}:rules[{index}]")
        for index, rule_raw in enumerate(rules_raw)
    )

    return ManifestRoute(
        route_id=route_id,
        route_kind=route_kind,  # type: ignore[arg-type]
        workload_type=workload_type,
        owner_system=owner_system,
        bot_identity=bot_identity,
        chat_id_binding=chat_id_binding,
        thread_mode=thread_mode,  # type: ignore[arg-type]
        thread_id_binding=thread_id_binding,
        rules=rules,
        enabled=enabled,
    )


def _parse_rule(raw: Any, *, source: str) -> ManifestRule:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: rule must be an object")
    rule_id = _required_str(raw, "ruleId", source)
    text_modes = frozenset(
        _required_enum({"value": value}, "value", VALID_TEXT_MODES, source)
        for value in _optional_str_list(raw, "textModes", source)
    )
    return ManifestRule(
        rule_id=rule_id,
        rule_version=_optional_str(raw, "ruleVersion") or "v1",
        command_names=frozenset(_optional_str_list(raw, "commandNames", source)),
        media_kinds=frozenset(_optional_str_list(raw, "mediaKinds", source)),
        text_modes=text_modes,  # type: ignore[arg-type]
        require_reply=_optional_bool(raw, "requireReply", source),
        allowed_sender_user_ids=frozenset(
            _optional_int_list(raw, "allowedSenderUserIds", source)
        ),
    )


def _binding_value(bindings: Mapping[str, int | None], binding: str, route_id: str) -> int | None:
    if binding not in bindings:
        raise ValueError(f"{route_id}: missing binding {binding}")
    value = bindings[binding]
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"{route_id}: binding {binding} must be an int")
    return value


def _required_str(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source}: missing required string field {key}")
    return value


def _optional_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_enum(
    raw: Mapping[str, Any],
    key: str,
    valid_values: frozenset[str],
    source: str,
) -> str:
    value = _required_str(raw, key, source)
    if value not in valid_values:
        valid = ", ".join(sorted(valid_values))
        raise ValueError(f"{source}: invalid {key} {value!r}; expected one of {valid}")
    return value


def _optional_enum(
    raw: Mapping[str, Any],
    key: str,
    valid_values: frozenset[str],
    source: str,
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in valid_values:
        valid = ", ".join(sorted(valid_values))
        raise ValueError(f"{source}: invalid {key} {value!r}; expected one of {valid}")
    return value


def _optional_str_list(raw: Mapping[str, Any], key: str, source: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source}: {key} must be a list of strings")
    return value


def _optional_int_list(raw: Mapping[str, Any], key: str, source: str) -> list[int]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise ValueError(f"{source}: {key} must be a list of integers")
    return value


def _optional_bool(raw: Mapping[str, Any], key: str, source: str) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{source}: {key} must be a boolean")
    return value
