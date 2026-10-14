"""Wellfound role pages for India. Jobs sit in the page's Apollo state; listing only, no apply."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

MAX_PAGES = 5  # about 35 jobs a page


class WellfoundAdapter(Adapter[BoardSearch]):
    portal = "wellfound"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        return f"https://wellfound.com/role/l/{search.slug}/india"

    def page_urls(self, search: BoardSearch) -> list[str]:
        base = self.url_for(search)
        return [base, *(f"{base}?page={n}" for n in range(2, MAX_PAGES + 1))]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        node = BeautifulSoup(text, "html.parser").find("script", id="__NEXT_DATA__")
        if node is None:
            raise ValueError("no __NEXT_DATA__ on page")
        state: dict[str, Any] = json.loads(node.get_text())["props"]["pageProps"]["apolloState"]
        data = state["data"]
        company_of = {
            ref["__ref"]: startup["name"]
            for key, startup in data.items() if key.startswith("StartupResult:")
            for ref in startup.get("highlightedJobListings") or []
        }  # fmt: skip

        def build(item: tuple[str, Any]) -> Posting | None:
            key, job = item
            if not key.startswith("JobListingSearchResult:"):
                return None
            location = ", ".join(job.get("locationNames") or [])
            if job.get("remote"):
                # A separate list item, so the India rule can tell open remote from limited.
                limits = "; ".join(job.get("acceptedRemoteLocationNames") or [])
                remote = f"Remote ({limits})" if limits else "Remote"
                location = f"{location} / {remote}" if location else remote
            low = job.get("yearsExperienceMin")
            started = job.get("liveStartAt")
            return Posting(
                source=self.portal,
                url=f"https://wellfound.com/jobs/{job['id']}-{job.get('slug') or 'job'}",
                company=company_of.get(key, ""),
                title=job["title"],
                location=location,
                experience_text=f"{low}+ years" if low is not None else "",
                posted_date=datetime.fromtimestamp(started, UTC).date() if started else None,
                description=strip_hidden_html(html.unescape(job.get("description") or "")),
            )

        return build_each(data.items(), build)
