"""remote.io's job list API. Its robots.txt disallows /api/ list calls; the owner chose on
08-11-2026 to read it anyway, a few requests a day. `term` is "IN" (India) or "worldwide"."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

API = "https://remote.io/api/v2/jobs"
PAGES = 3
SIZE = 50


class RemoteioAdapter(Adapter[BoardSearch]):
    portal = "remoteio"
    page_size = SIZE

    def _query(self, search: BoardSearch, page: int) -> dict[str, Any]:
        where = (
            {"region": "worldwide"}
            if search.term.lower() == "worldwide"
            else {"country": search.term.upper()}
        )
        return {"category": "software-development", "limit": SIZE, "page": page, **where}

    def url_for(self, search: BoardSearch) -> str:
        return f"{API}?{urlencode(self._query(search, 1))}"

    def page_urls(self, search: BoardSearch) -> list[str]:
        return [f"{API}?{urlencode(self._query(search, p))}" for p in range(1, PAGES + 1)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting | None:
            if job.get("isExpired") or job.get("isTestData"):
                return None
            where = str(job.get("location") or job.get("regionLabel") or "")
            if job.get("locationType") == "remote":
                where = remote_at(where)
            page = str(job.get("jobUrl") or "")
            return Posting(
                source=self.portal, url=f"https://remote.io{page}" if page else
                str(job.get("applicationUrl") or ""),
                company=str(job.get("companyName") or job.get("displayName") or ""),
                title=str(job.get("jobTitle") or ""), location=where,
                stack_words=[str(s) for s in job.get("skillTags") or []],
                posted_date=parse_date(job.get("publishedAt")),
                description=strip_hidden_html(str(job.get("description") or "")),
            )  # fmt: skip

        return build_each(parse_json(text)["data"], build)
