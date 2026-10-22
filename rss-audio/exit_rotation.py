"""Rotate the Mullvad egress IP used by the rss-audio proxy route.

gluetun's control server (loopback, token-authenticated) accepts VPN setting
updates, which force a WireGuard re-handshake to a different Mullvad server
without restarting the container.  That gives the dispatch loop a cheap
middle rung between "proxy healthy but the source refused us" and falling back
to the Mac route: up to ``max_rotations`` egress changes with conservative
jitter, then the route is abandoned for this job.

Rotations are deliberately bounded and trigger only for failure classes that
can plausibly be IP-shaped (transient network errors, auth required, unknown)
-- never for proxy-unavailable or format failures.  Each rotation costs a few
seconds and burns a Mullvad exit, and a misbehaving source must never pin the
worker in a rotation loop.

API surface (gluetun control server):
- GET  /v1/publicip/ip      -> {"public_ip": "1.2.3.4", ...}
- PUT  /v1/vpn/settings      body: {"provider": {"server_selection":
                             {"countries": ["usa"], "cities": ["Chicago IL"]}}}
                             (answers plain-text "running", not JSON)
City names are gluetun-normalized ("Chicago IL", "New York NY"); a bare
"chicago" is rejected with a 400 listing the valid choices.

Known limitation (observed live): gluetun's public-IP getter can fail right
after a server switch ("all fetchers failed") and stay empty for a long
while.  rotate() then reports False even though the exit did change, and the
caller falls back to the Mac route -- the designed bound, never a wrong
egress.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from source_audio import SourceFailureKind, classify_source_failure

logger = logging.getLogger(__name__)

# gluetun/Mullvad city names (state-suffixed, as in gluetun's servers list).
# Sparse on purpose: three rotations max, so a handful of cities suffice;
# gluetun's 24h updater re-selects freely among all USA servers between jobs.
US_CITIES = ("Boston MA", "New York NY", "Chicago IL", "Seattle WA", "Los Angeles CA")

ROTATION_ELIGIBLE = frozenset(
    {
        SourceFailureKind.NETWORK_TRANSIENT,
        SourceFailureKind.AUTH_REQUIRED,
        SourceFailureKind.SOURCE_UNAVAILABLE,  # includes "unavailable in your country"
        SourceFailureKind.UNKNOWN,
    }
)

_CONTROL_TIMEOUT_SEC = 10
_HANDSHAKE_TIMEOUT_SEC = 45.0


@dataclass(frozen=True)
class ExitRotator:
    """Bounded egress rotation against a gluetun control server."""

    control_url: str = "http://127.0.0.1:8000"
    token: str = ""
    country: str = "usa"
    proxy: str = ""
    egress_echo_url: str = "https://api.ipify.org"
    max_rotations: int = 3
    # Conservative on purpose: rotations only happen between failed attempts,
    # so there is no concurrency to herd -- a small uniform delay is about
    # letting the source-side block cool down, not the worker.
    jitter_sec: tuple[float, float] = (8.0, 20.0)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def attempts(self) -> int:
        """Route attempts allowed: one, plus up to ``max_rotations`` retries
        when rotation is configured."""
        return self.max_rotations + 1 if self.enabled else 1

    def eligible(self, failure: str) -> bool:
        return classify_source_failure(failure) in ROTATION_ELIGIBLE

    def rotate(self, index: int) -> bool:
        """Ask gluetun to move to ``US_CITIES[index % len(US_CITIES)]`` and
        wait for the egress IP to change.

        Returns False when the control server is unreachable, the city is
        rejected, or the handshake did not land in time -- the caller then
        abandons the route for this job (today's failure behaviour).
        """
        if not self.enabled:
            return False
        city = US_CITIES[index % len(US_CITIES)]
        try:
            before = self._sample_egress()
            if before is None:
                raise ValueError("no egress source available")
            self._put_server_selection([city])
            if self._wait_for_change(before):
                logger.info("egress rotated to %s (%s -> ...)", city, before)
                return True
            logger.warning("egress rotation to %s did not change public IP", city)
            return False
        except (OSError, urllib.error.URLError, ValueError) as exc:
            logger.warning("exit rotation to %s failed: %s", city, exc)
            return False

    def _put_server_selection(self, cities: list[str]) -> None:
        payload: dict[str, Any] = {
            "provider": {
                "server_selection": {
                    "countries": [self.country],
                    "cities": cities,
                }
            }
        }
        # gluetun answers the settings PUT with plain-text "running", not JSON;
        # only the HTTP status matters here.
        self._request_json("PUT", "/v1/vpn/settings", payload, expect_json=False)

    def _public_ip(self) -> str:
        payload = self._request_json("GET", "/v1/publicip/ip")
        address = payload.get("public_ip")
        if not isinstance(address, str) or not address:
            raise ValueError("control server returned no public_ip")
        return address

    def _request_json(self, method: str, path: str, payload: Any = None, *, expect_json: bool = True) -> Any:
        request = urllib.request.Request(
            self.control_url.rstrip("/") + path,
            method=method,
        )
        request.add_header("X-Api-Key", self.token)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
            request.data = json.dumps(payload).encode("utf-8")
        with urllib.request.urlopen(request, timeout=_CONTROL_TIMEOUT_SEC) as response:
            if response.status >= 300:
                raise ValueError(f"control server rejected update: HTTP {response.status}")
            body = response.read().decode("utf-8")
        if not expect_json:
            return None
        if not body.strip():
            raise ValueError("control server returned an empty response")
        return json.loads(body)

    def _egress_echo(self) -> str:
        """Egress IP as seen through the proxy itself (ground truth for the
        route the worker actually uses; gluetun's getter can be empty for
        long stretches after a switch)."""
        if not self.proxy:
            raise ValueError("no proxy configured for egress echo")
        handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
        opener = urllib.request.build_opener(handler)
        with opener.open(self.egress_echo_url, timeout=_CONTROL_TIMEOUT_SEC) as response:
            address = response.read().decode("utf-8").strip()
        if not address:
            raise ValueError("egress echo returned an empty address")
        return address

    def _sample_egress(self) -> str | None:
        """Current egress: gluetun publicip when it answers, else the proxy
        echo.  None when both are unavailable."""
        try:
            return self._public_ip()
        except (OSError, urllib.error.URLError, ValueError):
            pass
        try:
            return self._egress_echo()
        except (OSError, urllib.error.URLError, ValueError):
            return None

    def _wait_for_change(self, before: str, timeout_sec: float = _HANDSHAKE_TIMEOUT_SEC) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                current = self._sample_egress()
            except (OSError, urllib.error.URLError, ValueError):
                current = None  # mid-handshake both sources may be briefly silent
            if current and current != before:
                return True
            time.sleep(1.0)
        return False