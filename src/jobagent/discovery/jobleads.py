"""JobLeads India search page. Jobs sit in Nuxt's packed state; /api/ paging is off limits."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.models import Posting

_WRAPPERS = {"ShallowReactive", "Reactive", "Ref", "ShallowRef"}


def _unpack(table: list[Any], index: Any) -> Any:
    """Nuxt payloads are one flat array whose values point at other slots by number."""
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(table):
        return None
    value = table[index]
    if isinstance(value, list):
        if value and isinstance(value[0], str):
            return _unpack(table, value[1]) if value[0] in _WRAPPERS and len(value) > 1 else None
        return [_unpack(table, v) for v in value]
    if isinstance(value, dict):
        return {k: _unpack(table, v) for k, v in value.items()}
    return value


def _slot(table: list[Any], index: Any, key: str) -> Any:
    """Step one key down without unpacking the whole (large) payload."""
    while isinstance(index, int) and 0 <= index < len(table):
        value = table[index]
        if isinstance(value, list) and value and value[0] in _WRAPPERS:
            index = value[1]
            continue
        return value[key] if isinstance(value, dict) else None
    return None


class JobleadsAdapter(Adapter[BoardSearch]):
    portal = "jobleads"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        query = urlencode({"keywords": search.term, "location_country": "IN"})
        return f"https://www.jobleads.com/in/jobs?{query}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        node = BeautifulSoup(text, "html.parser").find("script", attrs={"data-nuxt-data": True})
        if node is None:
            raise ValueError("no Nuxt data on page")
        table = json.loads(node.get_text())
        index: Any = 0
        for key in ("pinia", "vdb-search-store", "allJobs"):
            index = _slot(table, index, key)

        def build(job: dict[str, Any]) -> Posting:
            posted = job.get("validFrom")
            place = ", ".join(p for p in (job.get("location"), job.get("locationRegion")) if p)
            return Posting(
                source=self.portal,
                url=f"https://www.jobleads.com{job['jobLink']}",
                company=job.get("company") or "",
                title=job["title"],
                location=place,
                posted_date=datetime.fromtimestamp(posted, UTC).date() if posted else None,
            )

        return build_each(_unpack(table, index) or [], build)
