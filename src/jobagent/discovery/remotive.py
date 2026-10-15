"""Remotive public API. Terms: link back, name Remotive, at most 4 reads a day."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

# The public feed ignores `category` today and returns every kind of job
TECH = frozenset(
    {
        "software development",
        "artificial intelligence",
        "data and analytics",
        "devops / sysadmin",
        "qa",
    }
)


class RemotiveAdapter(Adapter[BoardSearch]):
    """`term` is a Remotive category slug, e.g. "software-dev"."""

    portal = "remotive"
    conditional = True  # an unchanged feed is not swept again

    def url_for(self, search: BoardSearch) -> str:
        return f"https://remotive.com/api/remote-jobs?category={search.slug}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting | None:
            if (job.get("category") or "").lower() not in TECH:
                return None
            return Posting(
                source=self.portal,
                url=job["url"],
                company=job["company_name"].strip(),
                title=job["title"],
                location=remote_at(job.get("candidate_required_location") or ""),
                stack_words=list(job.get("tags") or []),
                posted_date=parse_date(job.get("publication_date")),
                description=strip_hidden_html(job.get("description") or ""),
            )

        return build_each(parse_json(text)["jobs"], build)
