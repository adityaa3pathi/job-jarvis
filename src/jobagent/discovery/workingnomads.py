"""Working Nomads public feed: one list of recent remote jobs across all categories."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


class WorkingnomadsAdapter(Adapter[BoardSearch]):
    """`term` is the category kept from the feed, e.g. "development"."""

    portal = "workingnomads"
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        return "https://www.workingnomads.com/api/exposed_jobs/"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting | None:
            if (job.get("category_name") or "").lower() != search.term.lower():
                return None
            return Posting(
                source=self.portal,
                url=job["url"],
                company=job["company_name"],
                title=job["title"],
                location=remote_at(job.get("location") or ""),
                stack_words=[t.strip() for t in (job.get("tags") or "").split(",") if t.strip()],
                posted_date=parse_date(job.get("pub_date")),
                description=strip_hidden_html(job.get("description") or ""),
            )

        data = parse_json(text)
        if not isinstance(data, list):
            raise ValueError("expected a list of jobs")
        return build_each(data, build)
