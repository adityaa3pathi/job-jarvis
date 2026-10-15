"""Workable careers page reader. No keyless API exists, so this is treated as unstable."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.base import Adapter, Company, build_each
from jobagent.models import Posting


class WorkableAdapter(Adapter[Company]):
    portal = "workable"
    expect_json = False

    def url_for(self, company: Company) -> str:
        return f"https://apply.workable.com/{company.token}/"

    def parse(self, text: str, company: Company) -> list[Posting]:
        seen: set[str] = set()

        def build(link: Tag) -> Posting | None:
            href = str(link.get("href", ""))
            if f"/{company.token}/j/" not in href or href in seen:
                return None
            seen.add(href)
            card = link.find_parent("li") or link.parent
            title_el = card.select_one('[data-ui="job-title"]') if isinstance(card, Tag) else None
            loc_el = card.select_one('[data-ui="job-location"]') if isinstance(card, Tag) else None
            title = (title_el or link).get_text(" ", strip=True)
            if not title:
                return None
            return Posting(
                source=self.portal,
                url=f"https://apply.workable.com{href}",
                company=company.name,
                title=title,
                location=loc_el.get_text(" ", strip=True) if loc_el else "",
            )

        return build_each(BeautifulSoup(text, "html.parser").select('a[href*="/j/"]'), build)
