"""Workable careers page reader. No keyless API exists, so this is treated as unstable."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.base import Adapter, Company
from jobagent.models import Posting


class WorkableAdapter(Adapter[Company]):
    portal = "workable"
    expect_json = False

    def url_for(self, company: Company) -> str:
        return f"https://apply.workable.com/{company.token}/"

    def parse(self, text: str, company: Company) -> list[Posting]:
        soup = BeautifulSoup(text, "html.parser")
        postings: list[Posting] = []
        seen: set[str] = set()
        for link in soup.select('a[href*="/j/"]'):
            href = str(link.get("href", ""))
            if f"/{company.token}/j/" not in href or href in seen:
                continue
            seen.add(href)
            card = link.find_parent("li") or link.parent
            title_el = card.select_one('[data-ui="job-title"]') if isinstance(card, Tag) else None
            loc_el = card.select_one('[data-ui="job-location"]') if isinstance(card, Tag) else None
            title = (title_el or link).get_text(" ", strip=True)
            if not title:
                continue
            postings.append(
                Posting(
                    source=self.portal,
                    url=f"https://apply.workable.com{href}",
                    company=company.name,
                    title=title,
                    location=loc_el.get_text(" ", strip=True) if loc_el else "",
                )
            )
        return postings
