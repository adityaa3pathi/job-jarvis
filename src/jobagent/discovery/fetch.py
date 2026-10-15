"""HTTP fetcher with an address guard: https only, known hosts, no private ranges (U3)."""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from jobagent.discovery.base import FetchResult

KNOWN_ATS_HOSTS = frozenset(
    {
        "boards-api.greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io",
        "api.lever.co", "api.eu.lever.co", "jobs.lever.co", "jobs.eu.lever.co",
        "api.ashbyhq.com", "jobs.ashbyhq.com", "apply.workable.com", "jobs.workable.com",
        "api.smartrecruiters.com", "jobs.smartrecruiters.com",
    }
)  # fmt: skip
KNOWN_BOARD_HOSTS = frozenset(
    {
        "jobgether.com", "gladiator.hirist.tech", "www.hirist.tech", "cutshort.io",
        "bebee.com", "www.switchly.in", "www.jobleads.com", "wellfound.com", "www.ycombinator.com",
        "www.linkedin.com", "in.linkedin.com", "himalayas.app", "remoteok.com", "jobicy.com",
        "remotive.com", "weworkremotely.com", "www.workingnomads.com", "hn.algolia.com",
        "www.instahyre.com",
    }
)  # fmt: skip
MAX_REDIRECTS = 3
MAX_BYTES = 16_000_000  # the biggest Greenhouse boards with full job text reach ~12 MB
VALIDATORS = ("etag", "last-modified")
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
        deadline_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.resolver = resolver
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.deadline_s = deadline_s
        self.clock = clock
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            headers={"User-Agent": "job-apply-agent/0.1 (personal job search; low rate)"},
        )

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> FetchResult:
        # timeout_s bounds each read; deadline_s bounds the whole request incl. redirects
        end = self.clock() + self.deadline_s
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            guard_url(current, self.allowed_hosts, self.resolver)
            timeout = min(self.timeout_s, self._remaining(end))
            with self._client.stream("GET", current, timeout=timeout, headers=headers) as response:
                self._remaining(end)
                if response.is_redirect and response.status_code != 304:  # 304: not changed
                    location = response.headers.get("location")
                    if not location:
                        raise FetchRefused("redirect without location")
                    current = urljoin(current, location)
                    continue
                text = self._read_text(response, end)
                kept = {k: response.headers[k] for k in VALIDATORS if k in response.headers}
                return FetchResult(response.status_code, text, current, kept)
        raise FetchRefused("too many redirects")

    def _remaining(self, end: float) -> float:
        left = end - self.clock()
        if left <= 0:
            raise FetchRefused(f"request took longer than {self.deadline_s:g} s")
        return left

    def _read_text(self, response: httpx.Response, end: float) -> str:
        # One growing buffer decoded in place: peak is about body + text, not 3x body
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > self.max_bytes:
                raise FetchRefused("response too large")
            self._remaining(end)
        text = body.decode(response.encoding or "utf-8", errors="replace")
        del body
        return text
