"""Switchly public role pages. Each shows only the newest few jobs; full search needs a login."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.models import Posting


class SwitchlyAdapter(Adapter[BoardSearch]):
    portal = "switchly"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        return f"https://www.switchly.in/{search.slug}-jobs"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(link: Tag) -> Posting | None:
            lines = [p.get_text(" ", strip=True) for p in link.find_all("p")]
            if len(lines) < 2:
                return None
            company, _, location = lines[1].partition(" · ")
            return Posting(
                source=self.portal,
                url=f"https://www.switchly.in{link.get('href')}",
                company=company.strip(),
                title=lines[0],
                location=location.strip(),
            )

        return build_each(BeautifulSoup(text, "html.parser").select('a[href^="/j/"]'), build)
