"""Hirist search, read from the JSON endpoint its own search page calls (not documented)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, parse_json
from jobagent.models import Posting

MAX_PAGES = 1  # one 200-job page per term; the policy keeps 10 s between requests


class HiristAdapter(Adapter[BoardSearch]):
    portal = "hirist"

    def url_for(self, search: BoardSearch, page: int = 0) -> str:
        query = urlencode({"query": search.term, "page": page, "size": 200})
        return f"https://gladiator.hirist.tech/job/search?{query}"

    def page_urls(self, search: BoardSearch) -> list[str]:
        return [self.url_for(search, page) for page in range(MAX_PAGES)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        postings = []
        for job in parse_json(text)["data"]:
            places = [str(loc.get("name")) for loc in job.get("locations") or [] if loc.get("name")]
            if job.get("workFromHome"):
                places.append("Remote")
            created = job.get("createdTimeMs")
            low, high = job.get("min"), job.get("max")
            postings.append(
                Posting(
                    source=self.portal,
                    url=job["jobDetailUrl"],
                    company=(job.get("companyData") or {}).get("companyName") or "",
                    title=job.get("jobdesignation") or job["title"],
                    location=", ".join(places),
                    experience_text=f"{low}-{high} yrs" if low is not None else "",
                    stack_words=[t["name"] for t in job.get("tags") or [] if t.get("name")],
                    posted_date=datetime.fromtimestamp(created / 1000, UTC).date()
                    if created
                    else None,
                )
            )
        return postings
