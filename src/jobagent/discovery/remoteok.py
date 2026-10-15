"""RemoteOK public API. Its terms ask for a link back, so `url` stays the RemoteOK page."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


class RemoteokAdapter(Adapter[BoardSearch]):
    """`term` is a RemoteOK tag, e.g. "python" or "react"."""

    portal = "remoteok"

    def url_for(self, search: BoardSearch) -> str:
        return f"https://remoteok.com/api?tag={search.slug}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting:
            return Posting(
                source=self.portal,
                url=job["url"].replace("https://remoteOK.com/", "https://remoteok.com/"),
                company=job["company"],
                title=job["position"],
                location=remote_at(job.get("location") or ""),
                stack_words=list(job.get("tags") or []),
                posted_date=parse_date(job.get("date")),
                description=strip_hidden_html(job.get("description") or ""),
            )

        data = parse_json(text)
        if not isinstance(data, list):
            raise ValueError("expected a list of jobs")
        return build_each([j for j in data if "position" in j], build)  # item 0: legal notice
