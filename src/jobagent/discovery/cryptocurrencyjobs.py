"""Cryptocurrency Jobs public RSS feed."""

from __future__ import annotations

import html.entities
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from jobagent.discovery.base import Adapter, BoardSearch, build_each, remote_at
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


def _replace_entity(m: re.Match[str]) -> str:
    ent = m.group(1)
    if ent in ("amp", "lt", "gt", "quot", "apos"):
        return f"&{ent};"
    cp = html.entities.name2codepoint.get(ent)
    return f"&#{cp};" if cp else m.group(0)


def _text(item: ET.Element, tag: str) -> str:
    return (item.findtext(tag) or "").strip()


class CryptocurrencyjobsAdapter(Adapter[BoardSearch]):
    portal = "cryptocurrencyjobs"
    expect_json = False
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        return "https://cryptocurrencyjobs.co/index.xml"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(item: ET.Element) -> Posting | None:
            raw_title = _text(item, "title")
            if not raw_title:
                return None
            if " at " in raw_title:
                title, _, company = raw_title.rpartition(" at ")
            else:
                title, company = raw_title, ""
            url = _text(item, "link") or _text(item, "guid")
            pub_date = _text(item, "pubDate")
            posted = parsedate_to_datetime(pub_date).date() if pub_date else None
            desc = _text(item, "description")
            return Posting(
                source=self.portal,
                url=url,
                company=company.strip(),
                title=title.strip(),
                location=remote_at("Remote"),
                posted_date=posted,
                description=strip_hidden_html(desc),
            )

        clean = re.sub(r"&([a-zA-Z]+);", _replace_entity, text)
        return build_each(ET.fromstring(clean).iter("item"), build)
