"""Mullvad exit rotation: eligibility (pure) and the gluetun control server over real HTTP.

eligible: 403 / bot check / rate limit / geo block / unknown extractor error -> rotate;
          connection reset (route down) / no formats -> do not rotate; no token -> disabled, one attempt
rotate(i) against a local gluetun stand-in
| control state                  | outcome                                                      |
| accepts PUT, IP changes        | True; PUT {"countries": ["usa"], "cities": [city i]} + X-Api-Key |
| public-ip getter empty         | True, confirmed through the proxy egress echo                |
| PUT answered 400               | False                                                        |
"""
import pytest

import exit_rotation
from chain_harness import Edge
from exit_rotation import ExitRotator


def test_eligibility():
    rotator = ExitRotator()
    for text in ("HTTP Error 403: Forbidden", "Sign in to confirm you're not a bot", "too many requests",
                 "unavailable in your country", "unexpected extractor error"):
        assert rotator.eligible(text), text
    assert not rotator.eligible("connection reset") and not rotator.eligible("no video formats found")
    assert (rotator.enabled, rotator.attempts(), rotator.rotate(0)) == (False, 1, False)
    assert len(exit_rotation.US_CITIES) >= rotator.max_rotations == 3


def gluetun(getter_ip, put_status):
    edge, state = Edge(), {"ip": "1.1.1.1"}

    def put(req):
        if put_status != 200:
            return put_status, b""
        state["ip"] = "2.2.2.2"
        return 200, b"running", "text/plain"  # gluetun answers the PUT with plain text
    edge.route("PUT", r"^/v1/vpn/settings$", put)
    edge.route("GET", r"^/v1/publicip/ip$", lambda req: (200, {"public_ip": state["ip"] if getter_ip else ""}))
    edge.route("GET", r"^/ip$", lambda req: (200, state["ip"].encode(), "text/plain"))
    return edge


@pytest.mark.parametrize("index, getter_ip, put_status, rotated", [
    (1, True, 200, True), (2, False, 200, True), (0, True, 400, False)])
def test_rotate(index, getter_ip, put_status, rotated):
    edge = gluetun(getter_ip, put_status)
    try:
        rotator = ExitRotator(control_url=edge.url, token="secret", proxy=edge.url, egress_echo_url=f"{edge.url}/ip")
        assert rotator.rotate(index) is rotated
        if rotated:
            (put,) = [r for r in edge.requests if r.method == "PUT"]
            assert put.headers["x-api-key"] == "secret"
            assert put.json() == {"provider": {"server_selection": {
                "countries": ["usa"], "cities": [exit_rotation.US_CITIES[index]]}}}
    finally:
        edge.close()
