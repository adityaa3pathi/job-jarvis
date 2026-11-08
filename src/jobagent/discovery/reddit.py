"""A subreddit's [Hiring] posts through Reddit's search RSS (Atom), no account. Reddit ends
RSS on 13-11-2026, after which this reader only reports the feed as unreachable."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.discovery.social import social_posting
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

ATOM = "{http://www.w3.org/2005/Atom}"


class RedditAdapter(Adapter[BoardSearch]):
    """`term` is the subreddit name, e.g. "forhire"."""

    portal = "reddit"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        return (
            f"https://www.reddit.com/r/{search.term}/search.rss"
            "?q=flair%3AHiring&restrict_sr=1&sort=new"
        )

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(entry: ET.Element) -> Posting | None:
            link = entry.find(f"{ATOM}link")
            title = entry.findtext(f"{ATOM}title") or ""
            body = strip_hidden_html(entry.findtext(f"{ATOM}content") or "")
            author = (entry.findtext(f"{ATOM}author/{ATOM}name") or "").strip()
            return social_posting(
                source=self.portal, url=link.get("href", "") if link is not None else "",
                text=f"{title}\n{body}", fallback_company=f"r/{search.term}: {author}".strip(),
                posted=entry.findtext(f"{ATOM}updated") or entry.findtext(f"{ATOM}published"),
            )  # fmt: skip

        return build_each(ET.fromstring(text).iter(f"{ATOM}entry"), build)
