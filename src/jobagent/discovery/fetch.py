"""HTTP fetcher with an address guard: https only, known hosts, no private ranges (U3)."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from jobagent.discovery.base import FetchResult

KNOWN_ATS_HOSTS = frozenset(
    {
        "boards-api.greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io",
        "api.lever.co", "api.eu.lever.co", "jobs.lever.co", "jobs.eu.lever.co",
        "api.ashbyhq.com", "jobs.ashbyhq.com", "apply.workable.com", "jobs.workable.com",
    }
)  # fmt: skip
KNOWN_BOARD_HOSTS = frozenset(
    {
        "jobgether.com", "gladiator.hirist.tech", "www.hirist.tech", "cutshort.io",
        "bebee.com", "www.switchly.in", "www.jobleads.com", "wellfound.com", "www.ycombinator.com",
    }
)  # fmt: skip
MAX_REDIRECTS = 3
MAX_BYTES = 16_000_000  # the biggest Greenhouse boards with full job text reach ~12 MB
Resolver = Callable[[str, int], list[str]]


class FetchRefused(Exception):
    pass


def system_resolver(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def _bad_address(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )  # fmt: skip


def check_scheme(url: str) -> str:
    """Refuse anything but https on port 443. Returns the lowercase host."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefused(f"scheme not allowed: {parts.scheme or 'none'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise FetchRefused("url has no host")
    if (parts.port or 443) != 443:
        raise FetchRefused(f"port not allowed: {parts.port}")
    return host


def guard_url(url: str, allowed_hosts: Iterable[str], resolve: Resolver) -> str:
    """Return the host if the URL is safe to fetch, else raise FetchRefused."""
    host = check_scheme(url)
    if host not in {h.lower() for h in allowed_hosts}:
        raise FetchRefused(f"host not allowed: {host}")
    try:
        addresses = resolve(host, 443)
    except OSError as exc:
        raise FetchRefused(f"cannot resolve {host}") from exc
    if not addresses or any(_bad_address(a) for a in addresses):
        raise FetchRefused(f"host resolves to a private or reserved address: {host}")
    return host


class HttpFetcher:
    def __init__(
        self,
        allowed_hosts: Iterable[str] = KNOWN_ATS_HOSTS | KNOWN_BOARD_HOSTS,
        resolver: Resolver = system_resolver,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 15.0,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.resolver = resolver
        self.max_bytes = max_bytes
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            headers={"User-Agent": "job-apply-agent/0.1 (personal job search; low rate)"},
        )

    def get(self, url: str) -> FetchResult:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            guard_url(current, self.allowed_hosts, self.resolver)
            with self._client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchRefused("redirect without location")
                    current = urljoin(current, location)
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > self.max_bytes:
                        raise FetchRefused("response too large")
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                return FetchResult(response.status_code, text, current)
        raise FetchRefused("too many redirects")
