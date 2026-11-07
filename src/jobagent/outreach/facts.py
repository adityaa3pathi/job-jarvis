"""Two true, specific things to mention in a note. The AI picks them; a quote check drops any
fact whose quote is not in the source text. Names and emails are never sent to the AI."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from jobagent.llm import LLM, LLMCache, LLMError, ask, cache_key, data_block, model_id
from jobagent.outreach.emails import EMAIL_RE
from jobagent.sanitize import clean_posting_text

STEP = "outreach_facts"
PROMPT_VERSION = "facts-1"
MAX_JOB_CHARS = 8_000
MAX_SITE_CHARS = 6_000
KEEP = 2
SYSTEM = (
    "A job seeker will write a short personal note to someone at this company. From the JOB "
    "and COMPANY blocks, pick up to 3 specific facts worth mentioning: a product, a recent "
    "launch, a customer, or a technical problem the job names. Skip generic praise and perks. "
    "Each fact is under 20 words. Each quote is 4 to 30 words copied exactly from the block "
    'named in "from". Never invent anything. Return JSON exactly as {"facts": [{"fact": str, '
    '"quote": str, "from": "job" | "company"}]}.'
)


class Fact(BaseModel):
    fact: str = Field(max_length=300)
    quote: str = Field(max_length=600)
    source: Literal["job", "company"] = Field(alias="from")

    model_config = {"populate_by_name": True}


class _Reply(BaseModel):
    facts: list[Fact] = Field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def grounded(facts: list[Fact], job: str, company: str) -> list[Fact]:
    """Facts whose quote really appears in the block it names, at most KEEP."""
    sources = {"job": _norm(job), "company": _norm(company)}
    kept = [f for f in facts if len(_norm(f.quote).split()) >= 4
            and _norm(f.quote) in sources[f.source]]  # fmt: skip
    return kept[:KEEP]


def pick_facts(
    job_text: str, site_text: str, llm: LLM, cache: LLMCache | None = None
) -> list[Fact]:
    """Empty when the AI fails or nothing checks out; the owner then writes the hook."""
    job, _ = clean_posting_text(job_text[: MAX_JOB_CHARS * 2])
    site, _ = clean_posting_text(site_text[: MAX_SITE_CHARS * 2]) if site_text else ("", False)
    job = EMAIL_RE.sub("[email]", job)[:MAX_JOB_CHARS]
    site = EMAIL_RE.sub("[email]", site)[:MAX_SITE_CHARS]
    key = cache_key(STEP, PROMPT_VERSION, model_id(llm), job, site)
    if cache is not None and (hit := cache.cache_get(key)):
        return [Fact.model_validate(f) for f in json.loads(hit)]
    user = data_block("JOB", job) + "\n" + data_block("COMPANY", site or "(not available)")
    try:
        reply = ask(llm, _Reply, system=SYSTEM, user=user, step=STEP, version=PROMPT_VERSION)
    except LLMError:
        return []
    facts = grounded(reply.facts, job, site)
    if cache is not None:
        cache.cache_put(key, STEP, json.dumps([f.model_dump(by_alias=True) for f in facts]))
    return facts
