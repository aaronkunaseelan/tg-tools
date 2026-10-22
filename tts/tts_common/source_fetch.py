"""Guarded HTTP fetch for TTS source URLs.

Source URLs arrive in chat messages, so a fetch must never reach a private
address:

* only http/https, no userinfo;
* every address a host resolves to must be public, and the connection goes to
  the address that was checked (no second lookup to rebind);
* every redirect hop is checked again, at most ``MAX_REDIRECTS``;
* no proxy, a byte cap enforced while streaming, one overall deadline.

Failures carry a ``failure_class`` that ``typed_fetch`` maps onto the frozen
TTS taxonomy.
"""

from __future__ import annotations

import email.message
import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5
SOCKET_TIMEOUT_S = 30
DEADLINE_S = 120
USER_AGENT = "Mozilla/5.0 (compatible; tg-tools-tts/1)"
_REDIRECTS = {301, 302, 303, 307, 308}
_CHUNK = 64 * 1024


class FetchFailure(RuntimeError):
    def __init__(self, failure_class: str, message: str, *, temporary: bool = False) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.temporary = temporary


@dataclass(frozen=True)
class FetchResult:
    data: bytes
    final_url: str
    content_type: str
    filename: str
    sha256: str


def address_allowed(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _check_url(url: str) -> tuple[str, str, int, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise FetchFailure("invalid_url", f"not an http(s) URL: {url[:200]}")
    if parts.username is not None or parts.password is not None:
        raise FetchFailure("invalid_url", "URL carries credentials")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise FetchFailure("invalid_url", f"bad port: {exc}") from exc
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return scheme, parts.hostname, port, target


def _guarded_connect(address, timeout=SOCKET_TIMEOUT_S, source_address=None):
    """Replacement for socket.create_connection: resolve once, refuse any
    non-public answer, connect to the checked address."""
    host, port = address
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchFailure("network_transient", f"cannot resolve {host}: {exc}", temporary=True) from exc
    if not infos:
        raise FetchFailure("network_transient", f"{host} has no addresses", temporary=True)
    blocked = [info[4][0] for info in infos if not address_allowed(info[4][0])]
    if blocked:
        raise FetchFailure("blocked_fetch_target", f"{host} resolves to a non-public address ({blocked[0]})")
    last: OSError | None = None
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError(f"cannot connect to {host}")


def _connection(scheme: str, host: str, port: int) -> http.client.HTTPConnection:
    if scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=SOCKET_TIMEOUT_S, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=SOCKET_TIMEOUT_S)
    conn._create_connection = _guarded_connect  # type: ignore[attr-defined]
    return conn


def _status_failure(status: int) -> FetchFailure:
    if status in {401, 403, 407, 451}:
        return FetchFailure("http_forbidden", f"HTTP {status}")
    if status in {404, 410}:
        return FetchFailure("http_not_found", f"HTTP {status}")
    if status == 429:
        return FetchFailure("http_rate_limited", "HTTP 429", temporary=True)
    if status >= 500:
        return FetchFailure("http_server_error", f"HTTP {status}", temporary=True)
    return FetchFailure("http_client_error", f"HTTP {status}")


def _read_body(response: http.client.HTTPResponse, max_bytes: int, deadline: float) -> bytes:
    encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding not in {"identity", "gzip", "x-gzip", "deflate"}:
        raise FetchFailure("http_client_error", f"unsupported content encoding {encoding}")
    decoder = None if encoding == "identity" else zlib.decompressobj(
        16 + zlib.MAX_WBITS if "gzip" in encoding else zlib.MAX_WBITS)
    out = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise FetchFailure("timeout", "fetch deadline exceeded", temporary=True)
        chunk = response.read(_CHUNK)
        if not chunk:
            break
        if decoder is not None:
            # Bound the decoded size too: a small gzip body can expand enormously.
            chunk = decoder.decompress(chunk, max_bytes + 1 - len(out))
        out += chunk
        if len(out) > max_bytes:
            raise FetchFailure("byte_cap_exceeded", f"source exceeds {max_bytes} bytes")
    if decoder is not None:
        out += decoder.flush()
        if len(out) > max_bytes:
            raise FetchFailure("byte_cap_exceeded", f"source exceeds {max_bytes} bytes")
    return bytes(out)


def _filename(disposition: str) -> str:
    if not disposition:
        return ""
    message = email.message.Message()
    message["Content-Disposition"] = disposition
    name = message.get_filename() or ""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def fetch(url: str, *, max_bytes: int) -> FetchResult:
    deadline = time.monotonic() + DEADLINE_S
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, target = _check_url(current)
        conn = _connection(scheme, host, port)
        try:
            conn.request("GET", target, headers={
                "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"})
            response = conn.getresponse()
            if response.status in _REDIRECTS:
                location = response.getheader("Location")
                if not location:
                    raise FetchFailure("http_client_error", f"HTTP {response.status} without Location")
                current = urljoin(current, location.strip())
                continue
            if not 200 <= response.status < 300:
                raise _status_failure(response.status)
            declared = response.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchFailure("byte_cap_exceeded", f"Content-Length {declared} exceeds {max_bytes} bytes")
            data = _read_body(response, max_bytes, deadline)
            return FetchResult(
                data=data,
                final_url=current,
                content_type=response.getheader("Content-Type") or "",
                filename=_filename(response.getheader("Content-Disposition") or ""),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        except FetchFailure:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise FetchFailure("tls_error", f"TLS verification failed for {host}: {exc}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise FetchFailure("timeout", f"timed out fetching {host}", temporary=True) from exc
        except (OSError, http.client.HTTPException, zlib.error) as exc:
            raise FetchFailure("network_transient", f"{type(exc).__name__}: {exc}", temporary=True) from exc
        finally:
            conn.close()
    raise FetchFailure("too_many_redirects", f"more than {MAX_REDIRECTS} redirects")
