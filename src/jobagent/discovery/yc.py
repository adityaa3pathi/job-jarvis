"""YC's public job board (ycombinator.com/jobs). Work at a Startup itself needs a login."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.models import Posting

_PAGE_DATA = re.compile(r'data-page="([^"]+)"')


class YcAdapter(Adapter[BoardSearch]):
    """`term` is the location slug on YC's board, e.g. "india" or "remote"."""

    portal = "yc"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        return f"https://www.ycombinator.com/jobs/role/software-engineer/{search.slug}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        match = _PAGE_DATA.search(text)
        if match is None:
            raise ValueError("no page data on YC jobs page")
        props: dict[str, Any] = json.loads(html.unescape(match.group(1)))["props"]

        def build(job: dict[str, Any]) -> Posting:
            return Posting(
                source=self.portal,
                url=f"https://www.ycombinator.com{job['url']}",
                company=job.get("companyName") or "",
                title=job["title"],
                location=job.get("location") or "",
                experience_text=job.get("minExperience") or "",
                stack_words=list(job.get("skills") or []),
            )

        return build_each(props["jobPostings"], build)
