"""LinkedIn's logged-out job list (owner decision 15-10-2026). Read only, never logged in."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.models import Posting

LIST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
MAX_PAGES = 3  # 10 cards a page


def _text(card: Tag, selector: str) -> str:
    node = card.select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def job_link(href: str) -> str:
    """The job's own page without tracking params; the id sits in the path."""
    parts = urlsplit(href)
    if not parts.hostname or not parts.hostname.endswith("linkedin.com"):
        raise ValueError(f"not a LinkedIn link: {href!r}")
    if not parts.path.startswith("/jobs/view/"):
        raise ValueError(f"not a job link: {href!r}")
    return f"https://{parts.hostname}{parts.path}"


class LinkedinAdapter(Adapter[BoardSearch]):
    portal = "linkedin"
    expect_json = False
    page_size = 10

    def url_for(self, search: BoardSearch, start: int = 0) -> str:
        query = {"keywords": search.term, "location": "India", "start": start}
        return f"{LIST_URL}?{urlencode(query)}"

    def page_urls(self, search: BoardSearch) -> list[str]:
        return [self.url_for(search, page * 10) for page in range(MAX_PAGES)]

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(card: Tag) -> Posting:
            # Cards are either a link themselves or a div holding a full-card link
            link = card if card.name == "a" else card.select_one("a.base-card__full-link")
            listed = card.select_one("time[class*=job-search-card__listdate]")
            posted = str(listed.get("datetime") or "") if listed else ""
            return Posting(
                source=self.portal,
                url=job_link(str(link.get("href") or "") if link else ""),
                company=_text(card, ".base-search-card__subtitle"),
                title=_text(card, ".base-search-card__title"),
                location=_text(card, ".job-search-card__location"),
                posted_date=date.fromisoformat(posted) if posted else None,
            )

        cards = BeautifulSoup(text, "html.parser").select(".base-search-card")
        return build_each(cards, build)
