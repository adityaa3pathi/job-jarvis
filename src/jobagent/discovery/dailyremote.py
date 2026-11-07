"""DailyRemote remote jobs reader via Schema.org JSON-LD and role pages."""

from __future__ import annotations

import json
import re
from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each, remote_at
from jobagent.models import Posting


class DailyremoteAdapter(Adapter[BoardSearch]):
    portal = "dailyremote"
    expect_json = False
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        slug = search.slug or "developer"
        if not slug.endswith("-jobs"):
            slug = f"{slug}-jobs"
        return f"https://dailyremote.com/remote-{slug}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        scripts = re.findall(
            r"<script type=[\"']application/ld\+json[\"']>(.*?)</script>", text, re.DOTALL
        )
        items: list[dict[str, Any]] = []
        for s in scripts:
            try:
                data = json.loads(s)
                if data.get("@type") == "ItemList":
                    items.extend(data.get("itemListElement") or [])
            except Exception:
                continue

        def build(it: dict[str, Any]) -> Posting | None:
            name = (it.get("name") or "").strip()
            url = (it.get("url") or "").strip()
            if not name or not url:
                return None
            company = ""
            title = name
            if " - " in name:
                title, _, company = name.rpartition(" - ")
            elif " at " in name:
                title, _, company = name.rpartition(" at ")
            return Posting(
                source=self.portal,
                url=url,
                company=company.strip(),
                title=title.strip(),
                location=remote_at("Remote"),
            )

        return build_each(items, build)
