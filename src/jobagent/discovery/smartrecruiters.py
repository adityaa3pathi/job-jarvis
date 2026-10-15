"""SmartRecruiters public postings API, India jobs only (`country=in`)."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, Company, Postings, build_each, parse_json
from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html

API = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100  # the API's cap
MAX_PAGES = 3
_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


class SmartrecruitersAdapter(Adapter[Company]):
    """The list leaves out job text, so it is fetched per new job (as for Greenhouse)."""

    portal = "smartrecruiters"
    page_size = PAGE_SIZE

    def url_for(self, company: Company, offset: int = 0) -> str:
        return f"{API}/{company.token}/postings?country=in&limit={PAGE_SIZE}&offset={offset}"

    def page_urls(self, company: Company) -> list[str]:
        return [self.url_for(company, n * PAGE_SIZE) for n in range(MAX_PAGES)]

    def parse(self, text: str, company: Company) -> list[Posting]:
        detail: dict[str, str] = {}

        def build(job: dict[str, Any]) -> Posting:
            where = job.get("location") or {}
            location = where.get("fullLocation") or where.get("city") or ""
            posting = Posting(
                source=self.portal,
                url=f"https://jobs.smartrecruiters.com/{company.token}/{int(job['id'])}",
                company=company.name,
                title=job["name"],
                location=f"Remote, {location}" if where.get("remote") else location,
                experience_text=(job.get("experienceLevel") or {}).get("label") or "",
                posted_date=parse_date(job.get("releasedDate")),
            )
            detail[posting.url] = f"{API}/{company.token}/postings/{int(job['id'])}"
            return posting

        data = parse_json(text)
        out: Postings = build_each(data["content"], build)
        out.detail = detail
        total, offset = data.get("totalFound"), data.get("offset")
        if isinstance(total, int) and isinstance(offset, int):
            out.more = offset + len(data["content"]) < total
        return out

    def parse_detail(self, text: str) -> str:
        sections = (parse_json(text).get("jobAd") or {}).get("sections") or {}
        parts = [(sections.get(name) or {}).get("text") or "" for name in _SECTIONS]
        return "\n".join(strip_hidden_html(p) for p in parts if p)
