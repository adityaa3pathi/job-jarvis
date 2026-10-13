"""Ashby public job posting API. Unlisted jobs are skipped."""

from __future__ import annotations

from jobagent.discovery.base import Adapter, Company, parse_json
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting


class AshbyAdapter(Adapter[Company]):
    portal = "ashby"

    def url_for(self, company: Company) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{company.token}"

    def parse(self, text: str, company: Company) -> list[Posting]:
        postings = []
        for job in parse_json(text)["jobs"]:
            if job.get("isListed") is False:
                continue
            location = job.get("location") or ""
            if job.get("isRemote") and "remote" not in location.lower():
                location = f"{location} (Remote)".strip()
            postings.append(
                Posting(
                    source=self.portal,
                    url=job["jobUrl"],
                    company=company.name,
                    title=job["title"],
                    location=location,
                    posted_date=parse_date(job.get("publishedAt")),
                    description=job.get("descriptionPlain") or "",
                )
            )
        return postings
