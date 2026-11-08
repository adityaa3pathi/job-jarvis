"""A Mastodon server's #hiring tag feed (RSS, no account). `term` is the server's host name."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.discovery.social import social_posting
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


class MastodonAdapter(Adapter[BoardSearch]):
    portal = "mastodon"
    expect_json = False
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        return f"https://{search.term}/tags/hiring.rss"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(item: ET.Element) -> Posting | None:
            link = (item.findtext("link") or "").strip()
            published = (item.findtext("pubDate") or "").strip()
            author = link.split("/@", 1)[1].split("/", 1)[0] if "/@" in link else search.term
            return social_posting(
                source=self.portal, url=link,
                text=strip_hidden_html(item.findtext("description") or ""),
                fallback_company=f"@{author}",
                posted=parsedate_to_datetime(published).date().isoformat() if published else None,
            )  # fmt: skip

        return build_each(ET.fromstring(text).iter("item"), build)
