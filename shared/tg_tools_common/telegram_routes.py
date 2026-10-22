from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RouteKind = Literal[
    "rss_publish_route",
    "meeting_note_route",
    "transcribe_audio_route",
    "tts_synthesize_route",
    "x_tts_explain_route",
]

ThreadMode = Literal["exact", "root", "any"]
TextMode = Literal["url", "non_command_text", "any_text"]
RouteResolutionStatus = Literal["matched", "unmatched", "ambiguous"]

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class TelegramUpdateShape:
    bot_identity: str
    chat_id: int
    message_id: int
    thread_id: int | None = None
    reply_to_message_id: int | None = None
    sender_user_id: int | None = None
    text: str = ""
    command: str | None = None
    media_kinds: frozenset[str] = field(default_factory=frozenset)
    is_reply: bool = False

    @property
    def has_url(self) -> bool:
        return bool(URL_RE.search(self.text or ""))


@dataclass(frozen=True)
class RouteRule:
    rule_id: str
    rule_version: str = "v1"
    command_names: frozenset[str] = field(default_factory=frozenset)
    media_kinds: frozenset[str] = field(default_factory=frozenset)
    text_modes: frozenset[TextMode] = field(default_factory=frozenset)
    require_reply: bool | None = None
    allowed_sender_user_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RouteDefinition:
    route_id: str
    route_kind: RouteKind
    workload_type: str
    owner_system: str
    bot_identity: str
    chat_id: int
    thread_mode: ThreadMode = "exact"
    thread_id: int | None = None
    rules: tuple[RouteRule, ...] = field(default_factory=tuple)
    enabled: bool = True


@dataclass(frozen=True)
class MatchedRoute:
    route_id: str
    route_kind: RouteKind
    workload_type: str
    owner_system: str
    matched_rule_id: str
    matched_rule_version: str


@dataclass(frozen=True)
class RouteResolution:
    status: RouteResolutionStatus
    route: MatchedRoute | None = None
    candidate_route_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.status == "matched"


def normalize_command(text: str) -> str | None:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    token = stripped.split(maxsplit=1)[0][1:]
    if not token:
        return None
    command = token.split("@", 1)[0].lower()
    return command or None


def telegram_source_key(shape: TelegramUpdateShape) -> str:
    return f"tg:{shape.bot_identity}:{shape.chat_id}:{shape.message_id}"


class TelegramRouteRegistry:
    def __init__(self, routes: list[RouteDefinition] | tuple[RouteDefinition, ...]) -> None:
        self._routes = tuple(routes)
        route_ids = [route.route_id for route in self._routes]
        duplicate_ids = sorted({route_id for route_id in route_ids if route_ids.count(route_id) > 1})
        if duplicate_ids:
            raise ValueError(f"duplicate route_id values: {', '.join(duplicate_ids)}")

    @property
    def routes(self) -> tuple[RouteDefinition, ...]:
        return self._routes

    def scoped_route_ids(self, shape: TelegramUpdateShape) -> tuple[str, ...]:
        """Return enabled routes owned by this bot in the message's chat/thread.

        Rule matching is intentionally excluded. This lets an ingress instance
        distinguish an unsupported update on its own route from an update meant
        for another bot that happens to share the same Telegram forum.
        """
        return tuple(
            route.route_id
            for route in self._routes
            if route.enabled
            and route.bot_identity == shape.bot_identity
            and route.chat_id == shape.chat_id
            and _thread_matches(route, shape)
        )

    def resolve(self, shape: TelegramUpdateShape) -> RouteResolution:
        matches: list[MatchedRoute] = []
        for route in self._routes:
            matched_rule = _match_route(route, shape)
            if matched_rule is None:
                continue
            matches.append(
                MatchedRoute(
                    route_id=route.route_id,
                    route_kind=route.route_kind,
                    workload_type=route.workload_type,
                    owner_system=route.owner_system,
                    matched_rule_id=matched_rule.rule_id,
                    matched_rule_version=matched_rule.rule_version,
                )
            )

        if len(matches) == 1:
            return RouteResolution(status="matched", route=matches[0], candidate_route_ids=(matches[0].route_id,))
        if not matches:
            return RouteResolution(status="unmatched", reason="no enabled route matched update")
        return RouteResolution(
            status="ambiguous",
            candidate_route_ids=tuple(match.route_id for match in matches),
            reason="multiple routes matched update",
        )


def _thread_matches(route: RouteDefinition, shape: TelegramUpdateShape) -> bool:
    if route.thread_mode == "any":
        return True
    if route.thread_mode == "root":
        return shape.thread_id is None
    return route.thread_id == shape.thread_id


def _match_route(route: RouteDefinition, shape: TelegramUpdateShape) -> RouteRule | None:
    if not route.enabled:
        return None
    if route.bot_identity != shape.bot_identity:
        return None
    if route.chat_id != shape.chat_id:
        return None
    if not _thread_matches(route, shape):
        return None

    rules = route.rules or (RouteRule(rule_id="channel-default"),)
    for rule in rules:
        if _rule_matches(rule, shape):
            return rule
    return None


def _rule_matches(rule: RouteRule, shape: TelegramUpdateShape) -> bool:
    if rule.allowed_sender_user_ids and shape.sender_user_id not in rule.allowed_sender_user_ids:
        return False
    if rule.require_reply is not None and rule.require_reply != shape.is_reply:
        return False

    conditions: list[bool] = []
    if rule.command_names:
        command_names = {command.lower().lstrip("/") for command in rule.command_names}
        conditions.append(shape.command is not None and shape.command.lower() in command_names)
    if rule.media_kinds:
        conditions.append(
            ("any" in rule.media_kinds and bool(shape.media_kinds))
            or bool(rule.media_kinds.intersection(shape.media_kinds))
        )
    for mode in rule.text_modes:
        if mode == "url":
            conditions.append(shape.has_url)
        elif mode == "non_command_text":
            conditions.append(bool((shape.text or "").strip()) and shape.command is None)
        elif mode == "any_text":
            conditions.append(bool((shape.text or "").strip()))

    return any(conditions) if conditions else True

