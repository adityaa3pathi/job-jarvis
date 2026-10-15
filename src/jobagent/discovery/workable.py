"""Workable public jobs widget API. The careers page itself builds its list with scripts."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, Company, build_each, parse_json
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting


class WorkableAdapter(Adapter[Company]):
    portal = "workable"

    def url_for(self, company: Company) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{company.token}"

    def parse(self, text: str, company: Company) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting:
            place = ", ".join(p for p in (job.get("city"), job.get("country")) if p)
            return Posting(
                source=self.portal,
                url=f"https://apply.workable.com/{company.token}/j/{job['shortcode']}/",
                company=company.name,
                title=job["title"],
                location=f"Remote, {place}" if job.get("telecommuting") else place,
                experience_text=job.get("experience") or "",
                posted_date=parse_date(job.get("published_on")),
            )

        return build_each(parse_json(text)["jobs"], build)
