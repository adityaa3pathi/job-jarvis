"""a16z's portfolio job board (jobs.a16z.com; robots.txt allows all). The page embeds its
first 25 matches for `?q=` as data, so `term` is the search text, e.g. "backend"."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting

_START = '{"id":"ats:'


def _field(record: str, name: str) -> Any:
    """One JSON value from a record cut out of the page's embedded data."""
    found = re.search(rf'"{name}":("(?:[^"\\]|\\.)*"|true|false|null|-?\d+)', record)
    return json.loads(found.group(1)) if found else None


def _labels(record: str, name: str) -> list[str]:
    found = re.search(rf'"{name}":\[(.*?)\]', record)
    return re.findall(r'"label":"([^"]+)"', found.group(1)) if found else []


class A16zAdapter(Adapter[BoardSearch]):
    portal = "a16z"
    expect_json = False
    page_size = 25  # the page shows at most this many matches

    def url_for(self, search: BoardSearch) -> str:
        return f"https://jobs.a16z.com/jobs?q={quote(search.term)}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        data = text.replace('\\"', '"')
        if _START not in data and "jobs.a16z.com" not in data:
            raise ValueError("no job data on the a16z page")

        def build(record: str) -> Posting | None:
            url, title = _field(record, "apply_url"), _field(record, "title")
            if not url or not title:
                return None
            where = _field(record, "location") or ""
            if _field(record, "remote") is True:
                where = f"Remote ({where})" if where else "Remote"
            return Posting(
                source=self.portal, url=url, company=_field(record, "company_name") or "",
                title=title, location=where, posted_date=parse_date(_field(record, "posted_at")),
                stack_words=_labels(record, "job_types"),
            )  # fmt: skip

        return build_each(data.split(_START)[1:], build)
