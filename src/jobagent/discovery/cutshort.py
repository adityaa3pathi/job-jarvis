"""Cutshort role list pages. Jobs sit in the page's Next.js data; free-text search is off limits."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from jobagent.discovery.base import Adapter, BoardSearch
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

CITIES = (
    "bangalore-bengaluru", "delhi-ncr-gurgaon-noida", "pune", "hyderabad", "mumbai", "chennai",
)  # fmt: skip


class CutshortAdapter(Adapter[BoardSearch]):
    portal = "cutshort"
    expect_json = False
    paged = False

    def url_for(self, search: BoardSearch) -> str:
        return f"https://cutshort.io/jobs/{search.slug}-jobs"

    def page_urls(self, search: BoardSearch) -> list[str]:
        # A role page shows 50 jobs; its city pages are the only allowed way to see more.
        base = self.url_for(search)
        return [base, *(f"{base}-in-{city}" for city in CITIES)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        node = BeautifulSoup(text, "html.parser").find("script", id="__NEXT_DATA__")
        if node is None:
            raise ValueError("no __NEXT_DATA__ on page")
        queries = json.loads(node.get_text())["props"]["pageProps"]["dehydratedState"]["queries"]
        jobs = queries[0]["state"]["data"]["data"]["pageData"]["jobs"]
        postings = []
        for job in jobs:
            facts = job.get("jobFactSummary") or {}
            postings.append(
                Posting(
                    source=self.portal,
                    url=job["publicUrl"],
                    company=(job.get("companyDetails") or {}).get("name") or "",
                    title=job["headline"],
                    location=job.get("locationsText") or "",
                    experience_text=facts.get("experience") or "",
                    stack_words=list(job.get("allSkills") or []),
                    posted_date=parse_date(facts.get("postedDate")),
                    description=strip_hidden_html(job.get("sanitizedComment") or ""),
                )
            )
        return postings
