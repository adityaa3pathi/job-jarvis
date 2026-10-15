"""Himalayas public jobs API. `country=IN` returns only jobs open to people in India."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

MAX_PAGES = 3
PAGE_SIZE = 20  # fixed by the API


class HimalayasAdapter(Adapter[BoardSearch]):
    portal = "himalayas"
    page_size = PAGE_SIZE

    def url_for(self, search: BoardSearch, page: int = 1) -> str:
        query = {"q": search.term, "country": "IN", "page": page}
        return f"https://himalayas.app/jobs/api/search?{urlencode(query)}"

    def page_urls(self, search: BoardSearch) -> list[str]:
        return [self.url_for(search, page) for page in range(1, MAX_PAGES + 1)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting:
            # No country list means the job is open worldwide
            where = "; ".join(job.get("locationRestrictions") or []) or "Worldwide"
            published = job.get("pubDate")
            return Posting(
                source=self.portal,
                url=job["guid"],
                company=job["companyName"],
                title=job["title"],
                location=remote_at(where),
                experience_text=", ".join(job.get("seniority") or []),
                stack_words=list(job.get("categories") or []),
                posted_date=datetime.fromtimestamp(int(published), UTC).date()
                if published
                else None,
                description=strip_hidden_html(job.get("description") or ""),
            )

        data = parse_json(text)
        out = build_each(data["jobs"], build)
        total, offset = data.get("totalCount"), data.get("offset")
        if isinstance(total, int) and isinstance(offset, int):
            out.more = offset + len(data["jobs"]) < total
        return out
