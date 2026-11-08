"""Public Telegram channels through their web preview (t.me/s/<channel>), which needs no
account or API key. `term` is "<channel>" or "<channel> <keyword>" for the preview's search."""

from __future__ import annotations

import html
import re
from urllib.parse import quote

from jobagent.discovery.base import Adapter, BoardSearch, build_each
from jobagent.discovery.social import social_posting
from jobagent.models import Posting

_POST = re.compile(r'data-post="(\w+/\d+)"')
_TEXT = re.compile(r"tgme_widget_message_text[^>]*>(.*?)</div>", re.S)
_TIME = re.compile(r'<time datetime="([^"]+)"')


def _plain(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


class TelegramAdapter(Adapter[BoardSearch]):
    portal = "telegram"
    expect_json = False

    def url_for(self, search: BoardSearch) -> str:
        channel, _, keyword = search.term.partition(" ")
        query = f"?q={quote(keyword.strip())}" if keyword.strip() else ""
        return f"https://t.me/s/{channel}{query}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        if "tgme_channel_info" not in text and "tgme_widget_message" not in text:
            raise ValueError("not a public channel preview")
        channel = search.term.partition(" ")[0]

        def build(block: str) -> Posting | None:
            post, body = _POST.search(block), _TEXT.search(block)
            if post is None or body is None:
                return None  # a photo or file with no text
            when = _TIME.search(block)
            return social_posting(
                source=self.portal, url=f"https://t.me/{post.group(1)}", text=_plain(body.group(1)),
                fallback_company=f"Telegram: {channel}", posted=when.group(1) if when else None,
            )  # fmt: skip

        # One block per message, so a text never pairs with a neighbour's link or date.
        return build_each(text.split('class="tgme_widget_message_wrap')[1:], build)
