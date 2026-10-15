"""We Work Remotely category RSS feeds (robots.txt allows them)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from jobagent.discovery.base import Adapter, BoardSearch, build_each, remote_at
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


def _text(item: ET.Element, tag: str) -> str:
    return (item.findtext(tag) or "").strip()


class WeworkremotelyAdapter(Adapter[BoardSearch]):
    """`term` is a category feed slug, e.g. "remote-back-end-programming-jobs"."""

    portal = "weworkremotely"
    expect_json = False
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        return f"https://weworkremotely.com/categories/{search.slug}.rss"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(item: ET.Element) -> Posting:
            company, _, title = _text(item, "title").partition(": ")
            published = _text(item, "pubDate")
            return Posting(
                source=self.portal,
                url=_text(item, "link"),
                company=company if title else "",
                title=title or company,
                location=remote_at(_text(item, "region")),
                stack_words=[s for s in _text(item, "skills").split(",") if s.strip()],
                posted_date=parsedate_to_datetime(published).date() if published else None,
                description=strip_hidden_html(_text(item, "description")),
            )

        return build_each(ET.fromstring(text).iter("item"), build)
