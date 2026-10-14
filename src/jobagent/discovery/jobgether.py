"""Jobgether public jobs API, documented for agents and allowed by its robots.txt."""

from __future__ import annotations

from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, parse_json
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting

MAX_PAGES = 4  # the API caps a page at 25 jobs


class JobgetherAdapter(Adapter[BoardSearch]):
    portal = "jobgether"

    def url_for(self, search: BoardSearch, page: int = 1) -> str:
        query = {"keyword": search.term, "locations": "india", "sort": "date", "limit": 25}
        return f"https://jobgether.com/astroapi/ai/jobs.json?{urlencode(query)}&page={page}"

    def page_urls(self, search: BoardSearch) -> list[str]:
        return [self.url_for(search, page) for page in range(1, MAX_PAGES + 1)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        postings = []
        for job in parse_json(text)["jobs"]:
            location = job.get("location") or ""
            if job.get("remote"):
                location = f"{location} ({job['remote']})".strip()
            postings.append(
                Posting(
                    source=self.portal,
                    url=job["url"],
                    company=job["company"],
                    title=job["title"],
                    location=location,
                    experience_text=job.get("experience") or "",
                    stack_words=list(job.get("jobFunctions") or []),
                    posted_date=parse_date(job.get("postedAt")),
                )
            )
        return postings
