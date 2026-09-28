"""AgentCore Platform v1.0"""

# TRV-C2-004 — facility_url fetcher (generate mode).
#
# Fetches the facility's public web page and reduces it to plain text for
# FacilityProfileNode. Hardened against SSRF (S-2 — user-supplied URL):
#   - http/https only, default ports only, no userinfo (`http://safe@evil/`)
#   - every resolved IP must be public (loopback / private / link-local /
#     reserved ranges are rejected)
#   - **DNS-rebinding proof**: the connection is PINNED to the IP that was
#     validated — the socket connects to that address directly while the
#     Host header / TLS SNI + certificate check keep the original hostname.
#     A resolver that answers differently at request time gains nothing,
#     because no second resolution ever happens.
#   - redirects are NOT followed (a redirect could bounce to an internal host)
#   - response capped at MAX_FETCH_BYTES, 10s timeout
#   - script/style stripped; tags reduced to text

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from html.parser import HTMLParser

MAX_FETCH_BYTES = 512 * 1024
FETCH_TIMEOUT_S = 10


class UrlFetchError(Exception):
    """Raised when the facility URL cannot be fetched safely."""


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.chunks)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _validate_url(url: str) -> tuple[urllib.parse.ParseResult, str, int]:
    """Validate the URL and resolve it ONCE.

    Returns (parsed, pinned_ip, port). Every resolved address must be public;
    the first one becomes the pinned connect target (see fetch_facility_text).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlFetchError(f"unsupported scheme '{parsed.scheme}' (http/https only)")
    if not parsed.hostname:
        raise UrlFetchError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UrlFetchError("userinfo in URL is not allowed (SSRF guard)")
    if parsed.port not in (None, 80, 443):
        raise UrlFetchError(f"non-standard port {parsed.port} is not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise UrlFetchError(f"DNS resolution failed: {e}") from e
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public(ip):
            raise UrlFetchError(f"resolved address {ip} is not public (SSRF guard)")
        ips.append(str(ip))
    if not ips:
        raise UrlFetchError("hostname resolved to no addresses")
    return parsed, ips[0], port


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a pinned IP with SNI/cert check on the hostname."""

    def __init__(self, ip: str, port: int, server_hostname: str, timeout: float) -> None:
        self._ssl_context = ssl.create_default_context()
        self._server_hostname = server_hostname
        super().__init__(ip, port, timeout=timeout, context=self._ssl_context)

    def connect(self) -> None:  # pinned IP; hostname only used for TLS
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(sock, server_hostname=self._server_hostname)


def _open_connection(parsed: urllib.parse.ParseResult, ip: str, port: int) -> http.client.HTTPConnection:
    """Connection factory (seam for tests)."""
    if parsed.scheme == "https":
        return _PinnedHTTPSConnection(ip, port, parsed.hostname, FETCH_TIMEOUT_S)
    return http.client.HTTPConnection(ip, port, timeout=FETCH_TIMEOUT_S)


def fetch_facility_text(url: str) -> str:
    """Fetch the facility page and return plain text (SSRF-hardened).

    :raises UrlFetchError: on any validation/network failure — callers turn
        this into a clean status=error (never a crash).
    """
    parsed, ip, port = _validate_url(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    host_header = parsed.hostname if port in (80, 443) else f"{parsed.hostname}:{port}"
    try:
        conn = _open_connection(parsed, ip, port)
        conn.request("GET", path, headers={
            "Host": host_header,
            "User-Agent": "trv-c2-004-agent/0.1",
            "Accept": "text/html, text/plain;q=0.9, */*;q=0.1",
        })
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise UrlFetchError(
                f"redirect (HTTP {response.status}) is not followed (SSRF guard) — "
                "pass the final URL directly")
        if response.status != 200:
            raise UrlFetchError(f"HTTP {response.status}")
        body = response.read(MAX_FETCH_BYTES + 1)
        conn.close()
    except UrlFetchError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as e:
        raise UrlFetchError(f"fetch failed: {e}") from e
    if len(body) > MAX_FETCH_BYTES:
        raise UrlFetchError(f"page exceeds {MAX_FETCH_BYTES} bytes")
    text = html_to_text(body.decode("utf-8", errors="replace"))
    if not text.strip():
        raise UrlFetchError("page contained no extractable text")
    return text
