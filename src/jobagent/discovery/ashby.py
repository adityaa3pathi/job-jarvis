"""Ashby public job posting API. Unlisted jobs are skipped."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, Company, build_each, parse_json
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting


class AshbyAdapter(Adapter[Company]):
    portal = "ashby"
    conditional = True

    def url_for(self, company: Company) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{company.token}"

    def parse(self, text: str, company: Company) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting | None:
            if job.get("isListed") is False:
                return None
            location = job.get("location") or ""
            if job.get("isRemote") and "remote" not in location.lower():
                location = f"{location} (Remote)".strip()
            return Posting(
                source=self.portal,
                url=job["jobUrl"],
                company=company.name,
                title=job["title"],
                location=location,
                posted_date=parse_date(job.get("publishedAt")),
                description=job.get("descriptionPlain") or "",
            )

        return build_each(parse_json(text)["jobs"], build)
