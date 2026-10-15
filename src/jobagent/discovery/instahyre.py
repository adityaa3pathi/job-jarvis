"""Instahyre job search data, logged out. Its pages are Cloudflare-walled; owner chose to read
this list anyway (15-10-2026): slow, read-only, never log in or apply."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json
from jobagent.models import Posting

PAGE_SIZE = 35  # fixed by the site
MAX_PAGES = 3


class InstahyreAdapter(Adapter[BoardSearch]):
    """`term` is a skill keyword, e.g. "python" or "full stack". The list has no job text."""

    portal = "instahyre"
    page_size = PAGE_SIZE

    def url_for(self, search: BoardSearch, offset: int = 0) -> str:
        query = {"skills": search.term, "limit": PAGE_SIZE, "offset": offset}
        return f"https://www.instahyre.com/api/v1/job_search?{urlencode(query)}"

    def page_urls(self, search: BoardSearch) -> list[str]:
        return [self.url_for(search, n * PAGE_SIZE) for n in range(MAX_PAGES)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting:
            return Posting(
                source=self.portal,
                url=job["public_url"],
                company=job["employer"]["company_name"],
                title=job["title"],
                location=job.get("locations") or "",
                stack_words=list(job.get("keywords") or []),
            )

        data = parse_json(text)
        out = build_each(data["objects"], build)
        out.more = bool((data.get("meta") or {}).get("next"))
        return out
