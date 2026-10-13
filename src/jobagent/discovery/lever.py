"""Lever public postings API. EU-hosted companies use a different host."""

from __future__ import annotations

from datetime import UTC, datetime

from jobagent.discovery.base import Adapter, Company, parse_json
from jobagent.models import Posting


class LeverAdapter(Adapter[Company]):
    portal = "lever"

    def url_for(self, company: Company) -> str:
        host = "api.eu.lever.co" if company.region == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{company.token}?mode=json"

    def parse(self, text: str, company: Company) -> list[Posting]:
        postings = []
        for job in parse_json(text):
            cats = job.get("categories") or {}
            created = job.get("createdAt")
            posted = datetime.fromtimestamp(created / 1000, UTC).date() if created else None
            postings.append(
                Posting(
                    source=self.portal,
                    url=job["hostedUrl"],
                    company=company.name,
                    title=job["text"],
                    location=cats.get("location") or ", ".join(cats.get("allLocations") or []),
                    posted_date=posted,
                    description=job.get("descriptionPlain") or "",
                )
            )
        return postings
