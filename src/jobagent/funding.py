"""Startup funding news from public RSS feeds. Jobs at a company that raised recently get
a "funded" badge; funded companies with no jobs here yet are listed for the owner to add."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from jobagent.db import bump_version
from jobagent.dedupe import normalize_company
from jobagent.discovery.base import Fetcher, http_error
from jobagent.policy import Policy
from jobagent.sqlconn import chunks

# Both robots.txt files allow these feeds (checked 12-11-2026).
FEEDS = {"inc42": "https://inc42.com/buzz/feed/", "entrackr": "https://entrackr.com/rss"}
FUNDED_DAYS = 120  # how long a raise still counts as "recently funded"
KEEP_DAYS = 365

_PREFIX = re.compile(
    r"^\s*(\[[^\]]{1,20}\]\s*|(exclusive|breaking|funding alert|update)\s*[:|-]\s*)+", re.I
)
# "InMobi Secures Naming Rights For ₹65 Cr" is money, but not a raise.
_NOT_RAISE = re.compile(r"^(naming|rights|order|contract|tender|award|licen[cs]e|nod|loan)\b", re.I)
_VERB = re.compile(
    r"\s+(?:is\s+)?(?:to\s+raise|raises|raised|bags|secures|lands|nets|picks\s+up|closes|"
    r"gets|receives|mops\s+up|scoops\s+up)\s+",
    re.I,
)
_MONEY = re.compile(
    r"\b(funding|round|series\s+[a-f]|seed|pre-series|pre-seed|mn|bn|cr|crore|lakh|million|"
    r"billion|investment)\b|[$₹]|\brs\.?\s*\d",
    re.I,
)
_FUND = re.compile(r"\b(fund|fund\s+of\s+funds|first\s+close|final\s+close)\b", re.I)
# Words that end a description before the name: "D2C menswear brand QB MEN".
_DESCRIPTOR = re.compile(
    r"^.*\b(startup|start-up|brand|platform|company|firm|maker|provider|player|unicorn|"
    r"app|marketplace|venture|enabler|operator|manufacturer|developer|soonicorn)\s+",
    re.I,
)


@dataclass(frozen=True)
class FundingNews:
    company: str
    headline: str
    url: str
    published: date | None
    source: str


@dataclass
class FundingRead:
    news: list[FundingNews] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def company_from_headline(headline: str) -> str | None:
    """The company in a "<company> raises <money>" headline, or None when it is not one."""
    text = html.unescape(headline).strip()
    if _FUND.search(text) or not _MONEY.search(text):
        return None
    head = _VERB.split(_PREFIX.sub("", text), maxsplit=1)
    if len(head) < 2 or _NOT_RAISE.match(head[1]):
        return None
    name = _DESCRIPTOR.sub("", head[0]).strip(" '\"‘’“”")
    name = re.split(r"[’']s\s+", name)[-1]  # "Anil Goteti’s Scapia" names Scapia
    words = name.split()
    if not words or len(words) > 5 or len(name) > 60:
        return None
    return name


def parse_feed(text: str, source: str) -> list[FundingNews]:
    out: list[FundingNews] = []
    for item in ET.fromstring(text).iter("item"):
        headline = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        company = company_from_headline(headline)
        if not company or not url.startswith("https://"):
            continue
        raw = (item.findtext("pubDate") or "").strip()
        try:
            published = parsedate_to_datetime(raw).date() if raw else None
        except (TypeError, ValueError):
            published = None
        out.append(FundingNews(company, html.unescape(headline), url, published, source))
    return out


def read_funding(fetcher: Fetcher, policy: Policy, feeds: Mapping[str, str] = FEEDS) -> FundingRead:
    """Each feed once; a feed that fails costs only its own news."""
    out = FundingRead()
    for source, url in feeds.items():
        decision = policy.begin(source, "discover")
        if not decision.allowed:
            out.notes.append(f"{source}: {decision.reason}")
            continue
        try:
            result = fetcher.get(url)
            if error := http_error(result):
                out.notes.append(f"{source}: {error}")
                continue
            out.news += parse_feed(result.text, source)
        except Exception as exc:  # network or layout change: skip this feed only
            out.notes.append(f"{source}: {type(exc).__name__}: {exc}")
    return out


class FundingStore:
    """Funding news in the jobs database (table: db.FUNDING_SCHEMA)."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save(self, news: Iterable[FundingNews], *, keep_days: int = KEEP_DAYS) -> int:
        """New headlines only (same link = same story); old ones are cleared. Returns new."""
        now = datetime.now(UTC)
        fresh: dict[str, FundingNews] = {}
        for n in news:
            if normalize_company(n.company):
                fresh.setdefault(n.url, n)
        for part in chunks(list(fresh), 1):
            sql = f"SELECT url FROM funding WHERE url IN ({','.join('?' * len(part))})"
            for r in self.conn.execute(sql, tuple(part)).fetchall():
                fresh.pop(r["url"], None)
        rows = [
            (normalize_company(n.company), n.company, n.headline[:300], n.url,
             n.published.isoformat() if n.published else None, n.source, now.isoformat())
            for n in fresh.values()
        ]  # fmt: skip
        cutoff = (now.date() - timedelta(days=keep_days)).isoformat()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO funding(norm_company, company, headline, url, published, source,"
                " seen_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING",
                rows,
            )
            self.conn.execute(
                "DELETE FROM funding WHERE coalesce(published, seen_at) < ?", (cutoff,)
            )
            if rows:
                bump_version(self.conn)
        return len(rows)

    def _recent(self, days: int) -> str:
        return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()

    def funded(self, companies: Iterable[str], days: int = FUNDED_DAYS) -> dict[str, Any]:
        """Company name → its newest recent raise, for the names that raised."""
        by_norm: dict[str, list[str]] = {}
        for name in companies:
            if norm := normalize_company(name):
                by_norm.setdefault(norm, []).append(name)
        out: dict[str, Any] = {}
        norms = list(by_norm)
        for part in chunks(norms, 1):
            rows = self.conn.execute(
                "SELECT norm_company, headline, url, published FROM funding"
                f" WHERE norm_company IN ({','.join('?' * len(part))})"
                " AND coalesce(published, seen_at) >= ? ORDER BY coalesce(published, seen_at)",
                (*part, self._recent(days)),
            ).fetchall()
            for r in rows:  # oldest first, so the newest raise wins
                view = {"headline": r["headline"], "url": r["url"], "published": r["published"]}
                for name in by_norm[r["norm_company"]]:
                    out[name] = view
        return out

    def recent(self, watched: Iterable[str], days: int = FUNDED_DAYS) -> list[dict[str, Any]]:
        """Recent raises, newest first, with each company's open jobs here and whether
        its board is already on the watch list."""
        watch = {normalize_company(n) for n in watched}
        open_jobs: dict[str, tuple[str, int]] = {}
        sql = "SELECT company, COUNT(*) AS n FROM jobs WHERE status='found' GROUP BY company"
        for r in self.conn.execute(sql).fetchall():
            norm = normalize_company(r["company"])
            name, n = open_jobs.get(norm, (r["company"], 0))
            open_jobs[norm] = (name, n + int(r["n"]))
        rows = self.conn.execute(
            "SELECT company, norm_company, headline, url, published, source FROM funding"
            " WHERE coalesce(published, seen_at) >= ?"
            " ORDER BY coalesce(published, seen_at) DESC, id DESC LIMIT 300",
            (self._recent(days),),
        ).fetchall()
        out = []
        for r in rows:
            job_company, n = open_jobs.get(r["norm_company"], ("", 0))
            out.append(
                {k: r[k] for k in ("company", "headline", "url", "published", "source")}
                | {"open_jobs": n, "job_company": job_company,
                   "watched": r["norm_company"] in watch}
            )  # fmt: skip
        return out
