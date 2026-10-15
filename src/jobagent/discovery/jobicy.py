"""Jobicy public remote jobs API. Its notice asks that apply links go to the Jobicy URL."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


class JobicyAdapter(Adapter[BoardSearch]):
    """`term` is a Jobicy tag, e.g. "python"."""

    portal = "jobicy"

    def url_for(self, search: BoardSearch) -> str:
        return (
            f"https://jobicy.com/api/v2/remote-jobs?{urlencode({'count': 50, 'tag': search.term})}"
        )

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(job: dict[str, Any]) -> Posting:
            return Posting(
                source=self.portal,
                url=job["url"],
                company=job["companyName"],
                title=job["jobTitle"],
                location=remote_at(job.get("jobGeo") or ""),
                experience_text=job.get("jobLevel") or "",
                stack_words=list(job.get("jobIndustry") or []),
                posted_date=parse_date(job.get("pubDate")),
                description=strip_hidden_html(job.get("jobDescription") or ""),
            )

        return build_each(parse_json(text).get("jobs") or [], build)
