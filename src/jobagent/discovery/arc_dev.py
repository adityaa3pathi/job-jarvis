"""Arc.dev remote jobs reader from Next.js server state."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each, remote_at
from jobagent.models import Posting


class ArcDevAdapter(Adapter[BoardSearch]):
    portal = "arc_dev"
    expect_json = False
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        slug = search.slug or "software-engineer"
        return f"https://arc.dev/remote-jobs/{slug}"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        match = re.search(r"<script[^>]*__NEXT_DATA__[^>]*>(.*?)</script>", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except Exception:
            return []
        props = data.get("props", {}).get("pageProps", {})
        jobs_list: list[dict[str, Any]] = []
        jobs_list.extend(props.get("arcJobs") or [])
        jobs_list.extend(props.get("externalJobs") or [])

        def build(job: dict[str, Any]) -> Posting | None:
            title = (job.get("title") or "").strip()
            if not title:
                return None
            company = (
                job.get("companyName")
                or (job.get("company") or {}).get("name")
                or ""
            ).strip()
            url_str = (job.get("urlString") or "").strip()
            if url_str.startswith("http"):
                url = url_str
            elif url_str.startswith("/"):
                url = f"https://arc.dev{url_str}"
            elif url_str:
                url = f"https://arc.dev/remote-jobs/{url_str}"
            else:
                return None

            posted_at = job.get("postedAt")
            posted_date = None
            if isinstance(posted_at, (int, float)) and posted_at > 0:
                try:
                    # In seconds (e.g. 1.7e9) or ms (> 1e11)
                    ts = posted_at / 1000.0 if posted_at > 1e11 else posted_at
                    posted_date = datetime.fromtimestamp(ts, UTC).date()
                except Exception:
                    pass

            countries = job.get("requiredCountries") or []
            where = "; ".join(countries) if countries else "Worldwide"
            cats = [
                c.get("name")
                for c in (job.get("categories") or [])
                if isinstance(c, dict) and c.get("name")
            ]
            return Posting(
                source=self.portal,
                url=url,
                company=company,
                title=title,
                location=remote_at(where),
                stack_words=cats,
                posted_date=posted_date,
            )

        return build_each(jobs_list, build)
