"""Two true, specific things to mention in a note, taken from the company's own pages (home,
changelog, blog, docs). Quoting the job post or the reader's own posts back reads as AI spam
in 2026, so a fact must quote a company page and must not also sit in the job post."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from jobagent.discovery.base import Fetcher
from jobagent.llm import LLM, LLMCache, LLMError, ask, cache_key, data_block, model_id, short_cause
from jobagent.outreach.emails import EMAIL_RE
from jobagent.sanitize import clean_posting_text

log = logging.getLogger(__name__)
STEP = "outreach_facts"
PROMPT_VERSION = "facts-2"
MAX_JOB_CHARS = 4_000
MAX_PAGE_CHARS = 3_000
MAX_PAGES = 5
KEEP = 2
# The pages a company writes about its own work, tried in this order.
PATHS = ("", "/changelog", "/blog", "/engineering", "/releases", "/docs", "/about")
SUBDOMAINS = ("blog", "engineering", "docs")
SYSTEM = (
    "A job seeker will write a short personal note to someone at this company. From the PAGE "
    "blocks (the company's own website), pick up to 3 specific facts about what the company "
    "builds or recently shipped: a product, a launch, a named feature, a customer, a technical "
    "choice. Skip slogans, perks, hiring text and anything that only restates the JOB block, "
    "which is context only. Each fact is under 20 words. Each quote is 4 to 30 words copied "
    'exactly from one PAGE block, and "page" is that block\'s url. Never invent anything. '
    'Return JSON exactly as {"facts": [{"fact": str, "quote": str, "page": str}]}.'
)


@dataclass(frozen=True)
class Page:
    url: str
    text: str


class Fact(BaseModel):
    fact: str = Field(max_length=300)
    quote: str = Field(max_length=600)
    page: str = Field(default="", max_length=500)
    source: Literal["job", "company"] = Field(default="company", alias="from")

    model_config = {"populate_by_name": True}


class _Reply(BaseModel):
    facts: list[Fact] = Field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _clean(raw: str, cap: int) -> str:
    text, _ = clean_posting_text(raw[: cap * 4])
    return EMAIL_RE.sub("[email]", text)[:cap]


def company_pages(fetcher_for: Callable[[str], Fetcher], domain: str) -> list[Page]:
    """Up to MAX_PAGES pages that answer 200, with distinct text; a missing one is skipped."""
    fetcher = fetcher_for(domain)
    urls = [f"https://{domain}{p}" for p in PATHS] + [f"https://{s}.{domain}" for s in SUBDOMAINS]
    pages: list[Page] = []
    seen: set[str] = set()
    for url in urls:
        if len(pages) >= MAX_PAGES:
            break
        try:
            got = fetcher.get(url)
        except Exception as exc:  # most companies have only some of these pages
            log.info("company page %s skipped: %s", url, short_cause(exc))
            continue
        text = _clean(got.text, MAX_PAGE_CHARS) if got.status == 200 else ""
        if len(text) > 200 and (key := _norm(text[:500])) not in seen:
            seen.add(key)
            pages.append(Page(url, text))
    return pages


def grounded(facts: list[Fact], job: str, pages: list[Page]) -> list[Fact]:
    """Facts quoting a company page word for word, never text the job post also says."""
    by_url = {p.url: _norm(p.text) for p in pages}
    every = " ".join(by_url.values())
    in_job = _norm(job)
    kept = []
    for f in facts:
        quote = _norm(f.quote)
        if len(quote.split()) < 4 or quote in in_job:
            continue
        if quote in by_url.get(f.page, "") or (f.page not in by_url and quote in every):
            kept.append(f.model_copy(update={"source": "company"}))
    return kept[:KEEP]


def pick_facts(
    job_text: str, pages: list[Page], llm: LLM, cache: LLMCache | None = None
) -> list[Fact]:
    """Empty when there are no company pages, the AI fails, or nothing checks out; the owner
    then writes the reason for writing themselves."""
    if not pages:
        return []
    job = _clean(job_text, MAX_JOB_CHARS)
    key = cache_key(STEP, PROMPT_VERSION, model_id(llm), job, *(p.url + p.text for p in pages))
    if cache is not None and (hit := cache.cache_get(key)):
        return [Fact.model_validate(f) for f in json.loads(hit)]
    blocks = [data_block(f"PAGE {p.url}", p.text) for p in pages]
    user = data_block("JOB", job) + "\n" + "\n".join(blocks)
    try:
        reply = ask(llm, _Reply, system=SYSTEM, user=user, step=STEP, version=PROMPT_VERSION)
    except LLMError:
        return []
    facts = grounded(reply.facts, job, pages)
    if cache is not None:
        cache.cache_put(key, STEP, json.dumps([f.model_dump(by_alias=True) for f in facts]))
    return facts
