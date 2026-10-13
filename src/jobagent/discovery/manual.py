"""Fetch one owner-pasted posting link. Unknown hosts wait for the owner (U3)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.base import Company, Fetcher, blocked_reason
from jobagent.discovery.fetch import (
    KNOWN_ATS_HOSTS,
    KNOWN_BOARD_HOSTS,
    FetchRefused,
    Resolver,
    check_scheme,
    guard_url,
)
from jobagent.models import Posting
from jobagent.policy import Policy
from jobagent.sanitize import strip_hidden_html


@dataclass(frozen=True)
class ManualResult:
    status: Literal["ok", "needs_owner_confirmation", "refused", "unreachable"]
    reason: str = ""
    posting: Posting | None = None


def trusted_hosts(companies: list[Company], confirmed: set[str] | None = None) -> set[str]:
    hosts = set(KNOWN_ATS_HOSTS | KNOWN_BOARD_HOSTS) | {h.lower() for h in (confirmed or set())}
    hosts |= {c.careers_host.lower() for c in companies if c.careers_host}
    return hosts


def _json_ld_job(soup: BeautifulSoup) -> dict[str, Any] | None:
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(node.string or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def parse_posting_page(url: str, html_text: str) -> Posting:
    soup = BeautifulSoup(html_text, "html.parser")
    job = _json_ld_job(soup)
    host = (re.sub(r"^www\.", "", url.split("/")[2]) if "//" in url else url).split(".")[0]
    if job:
        org = job.get("hiringOrganization") or {}
        place = job.get("jobLocation") or {}
        place = place[0] if isinstance(place, list) and place else place
        addr = place.get("address", {}) if isinstance(place, dict) else {}
        location = ", ".join(
            str(addr.get(k)) for k in ("addressLocality", "addressCountry") if addr.get(k)
        )
        posted = str(job.get("datePosted") or "")[:10]
        return Posting(
            source="manual",
            url=url,
            company=str(org.get("name") or host),
            title=str(job.get("title") or ""),
            location=location,
            posted_date=date.fromisoformat(posted)
            if re.fullmatch(r"\d{4}-\d\d-\d\d", posted)
            else None,
            description=strip_hidden_html(str(job.get("description") or "")),
        )
    og = soup.find("meta", property="og:title")
    og_title = og.get("content") if isinstance(og, Tag) else None
    page_title = soup.title.string if soup.title and soup.title.string else ""
    title = str(og_title or page_title)
    return Posting(
        source="manual",
        url=url,
        company=host,
        title=str(title).strip(),
        description=strip_hidden_html(html_text),
    )


def fetch_posting(
    url: str,
    *,
    companies: list[Company],
    fetcher: Fetcher,
    policy: Policy,
    resolver: Resolver,
    confirmed_hosts: set[str] | None = None,
) -> ManualResult:
    hosts = trusted_hosts(companies, confirmed_hosts)
    try:
        host = check_scheme(url)
        if host not in hosts:
            return ManualResult("needs_owner_confirmation", f"unknown host: {host}")
        guard_url(url, hosts, resolver)
    except FetchRefused as exc:
        return ManualResult("refused", str(exc))
    decision = policy.begin("manual", "discover")
    if not decision.allowed:
        return ManualResult("refused", decision.reason)
    try:
        result = fetcher.get(url)
    except FetchRefused as exc:
        return ManualResult("refused", str(exc))
    except Exception as exc:
        return ManualResult("unreachable", f"{type(exc).__name__}: {exc}")
    reason = blocked_reason(result, expect_json=False)
    if reason:
        return ManualResult("unreachable", reason)
    return ManualResult("ok", posting=parse_posting_page(url, result.text))
