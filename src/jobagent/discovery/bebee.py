"""beBee India role pages. robots.txt bans query strings, so only the first page is read."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.models import Posting


class BebeeAdapter(Adapter[BoardSearch]):
    portal = "bebee"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        return f"https://bebee.com/in/jobs/role/{search.slug}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(card: Tag) -> Posting | None:
            path = str(card.get("data-voice-url") or "")
            if not path.startswith("/"):
                return None
            snippet = card.select_one("p.line-clamp-2")
            return Posting(
                source=self.portal,
                url=f"https://bebee.com{path}",
                company=str(card.get("data-voice-company") or ""),
                title=str(card.get("data-voice-title") or ""),
                location=str(card.get("data-voice-location") or ""),
                description=snippet.get_text(" ", strip=True) if snippet else "",
            )

        cards = BeautifulSoup(text, "html.parser").select('article[data-voice-job="card"]')
        return build_each(cards, build)
