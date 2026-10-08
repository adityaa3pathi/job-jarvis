"""Greenhouse public job board API (KTD9). Read only; applying is browser driven."""

from __future__ import annotations

from datetime import date

from jobagent.discovery.base import Adapter, Company, parse_json
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


def parse_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class GreenhouseAdapter(Adapter):
    portal = "greenhouse"

    def url_for(self, company: Company) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{company.token}/jobs?content=true"

    def parse(self, text: str, company: Company) -> list[Posting]:
        postings = []
        for job in parse_json(text)["jobs"]:
            location = (job.get("location") or {}).get("name", "")
            postings.append(
                Posting(
                    source=self.portal,
                    # Hosted form URL, so dedupe and the filler see one canonical address.
                    url=f"https://job-boards.greenhouse.io/{company.token}/jobs/{job['id']}",
                    company=company.name,
                    title=job["title"],
                    location=location,
                    posted_date=parse_date(job.get("first_published") or job.get("updated_at")),
                    description=strip_hidden_html(job.get("content") or ""),
                )
            )
        return postings
