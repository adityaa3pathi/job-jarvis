"""Bluesky's public post search (api.bsky.app, no account). `term` is the search text."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json
from jobagent.discovery.social import social_posting
from jobagent.models import Posting

API = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
# Job-board bots repost listings job-jarvis already reads; people saying "we're hiring" stay.
BOT = re.compile(r"\bjobs?\b|jobs\.|hiring|careers|remote ?work|job ?alert|^remote", re.I)


class BlueskyAdapter(Adapter[BoardSearch]):
    portal = "bluesky"

    def url_for(self, search: BoardSearch) -> str:
        return f"{API}?{urlencode({'q': search.term, 'limit': 100, 'sort': 'latest'})}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        def build(post: dict[str, Any]) -> Posting | None:
            author = post.get("author") or {}
            handle = str(author.get("handle") or "")
            if BOT.search(f"{handle} {author.get('displayName') or ''}"):
                return None
            rkey = str(post.get("uri") or "").rsplit("/", 1)[-1]
            record = post.get("record") or {}
            return social_posting(
                source=self.portal, url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                text=str(record.get("text") or ""),
                fallback_company=str(author.get("displayName") or handle) or "Bluesky post",
                posted=record.get("createdAt"),
            )  # fmt: skip

        return build_each(parse_json(text)["posts"], build)
