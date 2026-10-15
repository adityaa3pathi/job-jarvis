"""Lever public postings API. EU-hosted companies use a different host."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobagent.discovery.base import Adapter, Company, build_each, parse_json
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


def full_description(job: dict[str, Any]) -> str:
    """Requirement lists often hold the years asked, so the rules must see them."""
    parts = [job.get("descriptionPlain") or ""]
    for item in job.get("lists") or []:
        parts += [item.get("text") or "", strip_hidden_html(item.get("content") or "")]
    parts.append(job.get("additionalPlain") or "")
    return "\n".join(p.strip() for p in parts if p and p.strip())


class LeverAdapter(Adapter[Company]):
    portal = "lever"
    conditional = True

    def url_for(self, company: Company) -> str:
        host = "api.eu.lever.co" if company.region == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{company.token}?mode=json"

    def parse(self, text: str, company: Company) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting:
            cats = job.get("categories") or {}
            created = job.get("createdAt")
            return Posting(
                source=self.portal,
                url=job["hostedUrl"],
                company=company.name,
                title=job["text"],
                location=cats.get("location") or ", ".join(cats.get("allLocations") or []),
                posted_date=datetime.fromtimestamp(created / 1000, UTC).date() if created else None,
                description=full_description(job),
            )

        jobs = parse_json(text)
        if not isinstance(jobs, list):
            raise ValueError("expected a list of postings")
        return build_each(jobs, build)
