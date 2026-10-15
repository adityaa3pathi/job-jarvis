"""Hacker News "Who is hiring?" monthly thread, read through the public Algolia HN API."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from jobagent.discovery.base import (
    Adapter,
    BoardSearch,
    DiscoveryResult,
    Fetcher,
    SweepContext,
    Unreachable,
    build_each,
    http_error,
    parse_json,
)
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.policy import Policy
from jobagent.rules import INDIA_MARKERS, _has_marker
from jobagent.sanitize import strip_hidden_html

API = "https://hn.algolia.com/api/v1"
THREADS = f"{API}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
TITLE = "Ask HN: Who is hiring?"
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_LINK = re.compile(r"https?:|\w\.[a-z]{2,}\b")  # a URL or bare domain, never a role
_NOT_ROLE = re.compile(
    r"^(?:full[- ]?time|part[- ]?time|contract|intern|onsite|on-site|hybrid|visa|[$€£\d])", re.I
)


def _where(segment: str) -> bool:
    return bool(_REMOTE.search(segment)) or _has_marker(segment, INDIA_MARKERS)


class HnAdapter(Adapter[BoardSearch]):
    """`term` is the keyword the thread's comments must contain, e.g. "remote"."""

    portal = "hn"
    page_size = 1000  # the API's cap per request
    _story = ""  # this month's thread; set by discover before the base loop reads it

    def url_for(self, search: BoardSearch) -> str:
        query = {"tags": f"comment,story_{self._story}", "query": search.term, "hitsPerPage": 1000}
        return f"{API}/search?{urlencode(query)}"

    def discover(
        self, target: BoardSearch, fetcher: Fetcher, policy: Policy, ctx: SweepContext | None = None
    ) -> DiscoveryResult:
        if policy.begin(self.portal, "discover").allowed:
            try:
                result = fetcher.get(THREADS)
                if error := http_error(result):
                    raise ValueError(error)
                hits = parse_json(result.text)["hits"]
                self._story = next(str(h["objectID"]) for h in hits if h["title"].startswith(TITLE))
            except Exception as exc:  # no thread found costs this board only
                out = DiscoveryResult()
                out.unreachable.append(Unreachable(target.name, THREADS, f"thread lookup: {exc!r}"))
                return out
        return super().discover(target, fetcher, policy, ctx)

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(hit: dict[str, Any]) -> Posting | None:
            if str(hit.get("parent_id")) != self._story:
                return None  # replies, not job posts
            body = hit.get("comment_text") or ""
            header = strip_hidden_html(body.split("<p>", 1)[0])
            parts = [p.strip() for p in header.split("|") if p.strip()]
            where = [p for p in parts if _where(p)]
            if len(parts) < 2 or not where:
                return None  # not in the usual "Company | Role | Place" form, or no remote/India
            roles = [
                p
                for p in parts[1:]
                if p not in where and not _LINK.search(p) and not _NOT_ROLE.match(p)
            ]
            return Posting(
                source=self.portal,
                url=f"https://news.ycombinator.com/item?id={int(hit['objectID'])}",
                company=" ".join(parts[0].split("(", 1)[0].split()),
                title=roles[0] if roles else " ".join(header.split())[:120],
                location=" | ".join(where),
                posted_date=parse_date(hit.get("created_at")),
                description=strip_hidden_html(body),
            )

        return build_each(parse_json(text)["hits"], build)
