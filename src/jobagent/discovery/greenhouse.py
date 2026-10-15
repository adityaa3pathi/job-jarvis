"""Greenhouse public job board API (KTD9). Read only; applying is browser driven."""

from __future__ import annotations

from datetime import date
from typing import Any

from jobagent.discovery.base import Adapter, Company, Postings, build_each, parse_json
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


def parse_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class GreenhouseAdapter(Adapter[Company]):
    """Reads the list without job text (13x smaller); text comes per new job (KTD3)."""

    portal = "greenhouse"
    conditional = True

    def url_for(self, company: Company) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{company.token}/jobs"

    def parse(self, text: str, company: Company) -> list[Posting]:
        detail: dict[str, str] = {}

        def build(job: dict[str, Any]) -> Posting:
            posting = Posting(
                source=self.portal,
                # Hosted form URL, so dedupe and the filler see one canonical address.
                url=f"https://job-boards.greenhouse.io/{company.token}/jobs/{job['id']}",
                company=company.name,
                title=job["title"],
                location=(job.get("location") or {}).get("name", ""),
                posted_date=parse_date(job.get("first_published") or job.get("updated_at")),
                description=strip_hidden_html(job.get("content") or ""),
            )
            if "content" not in job:
                base = f"https://boards-api.greenhouse.io/v1/boards/{company.token}/jobs"
                detail[posting.url] = f"{base}/{int(job['id'])}"
            return posting

        out: Postings = build_each(parse_json(text)["jobs"], build)
        out.detail = detail
        return out

    def parse_detail(self, text: str) -> str:
        return strip_hidden_html(parse_json(text).get("content") or "")
