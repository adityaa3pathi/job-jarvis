"""Job description analyzer: one extraction call, plain-code checks, at most one repair call."""

from __future__ import annotations

import json
from dataclasses import asdict

from jobagent.llm import LLM, LLMCache, LLMError, ask, cache_key, data_block, model_id
from jobagent.resume.jd import (
    JDProfile,
    Pass,
    SkillNeed,
    implicit_from,
    level_for,
    verified_years,
    vote,
    weight,
    work_mode,
)
from jobagent.resume.ontology import all_terms, mention
from jobagent.sanitize import clean_posting_text, states_candidate_fact

PROMPT_VERSION = "jd-2"
STEP = "jd_analysis"
MAX_RESPONSIBILITIES = 8

SYSTEM = (
    "You read one job posting and list what it asks for. Copy skills as the posting spells "
    "them. Per skill give importance: 'must' only when the posting calls it required or core "
    "to the role, else 'nice'. implicit_skills are things the role clearly needs but the "
    "posting never names."
)
EXTRACT = (
    'List the requirements, at most 8 responsibilities. Reply as {"role_title": str, '
    '"level": str, "years_min": int|null, "domain": str, "responsibilities": [str], '
    '"skills": [{"name": str, "importance": str}], "implicit_skills": [str]}.'
)
SEEN = (
    "Known terms seen in the text (some may be plain words, like 'rest'): {terms}. Include "
    "the ones the job asks for."
)
REPAIR = "Your last reply named no skill that appears in the posting. Read it again."


def _scan(text: str) -> list[str]:
    """Known terms the posting really contains (plain code, no model)."""
    return [t for t in all_terms() if mention(t, text)]


def _extract(llm: LLM, text: str, hint: str, step: str) -> Pass | None:
    user = data_block("job_posting", text) + "\n" + hint
    try:
        return ask(llm, Pass, system=SYSTEM, user=user, step=step)
    except LLMError:
        return None


def _to_json(profile: JDProfile) -> str:
    return json.dumps(asdict(profile))


def _from_json(raw: str) -> JDProfile:
    data = json.loads(raw)
    data["skills"] = [SkillNeed(**s) for s in data["skills"]]
    data["implicit_skills"] = [SkillNeed(**s) for s in data["implicit_skills"]]
    return JDProfile(**data)


def analyze_jd(posting: str, llm: LLM, cache: LLMCache | None = None) -> JDProfile:
    text, flagged = clean_posting_text(posting)
    key = cache_key(STEP, PROMPT_VERSION, model_id(llm), text)
    if cache is not None and (hit := cache.cache_get(key)):
        return _from_json(hit)
    profile = _analyze(text, flagged, llm)
    if cache is not None and not profile.degraded:
        cache.cache_put(key, STEP, _to_json(profile))
    return profile


def _analyze(text: str, flagged: bool, llm: LLM) -> JDProfile:
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    found = _scan(text)
    ask_for = EXTRACT + ("\n" + SEEN.format(terms=", ".join(found)) if found else "")
    lead = _extract(llm, text, ask_for, "jd_extract")
    calls, title = 1, (lead.role_title if lead else "") or first_line
    skills = vote([lead], text, title) if lead else []
    # One repair when the reply was unusable or names nothing the posting contains.
    if found and not skills:
        calls += 1
        if again := _extract(llm, text, ask_for + "\n" + REPAIR, "jd_repair"):
            lead, title = again, again.role_title or first_line
            skills = vote([lead], text, title)
    if lead is None:
        return _fallback(text, first_line, flagged)
    return JDProfile(
        role_title=title.strip()[:120],
        level=level_for(lead.level, title),
        years_min=verified_years(lead.years_min, text),
        work_mode=work_mode(text),
        domain=lead.domain.strip()[:80],
        responsibilities=duties(lead.responsibilities)[:MAX_RESPONSIBILITIES],
        skills=skills,
        implicit_skills=implicit_from([lead], text),
        injection_flagged=flagged,
        rounds=calls,
    )


def duties(lines: list[str]) -> list[str]:
    """Job duties only: a line about the candidate's own skills is never kept as a fact."""
    return [r.strip()[:200] for r in lines if r.strip() and not states_candidate_fact(r)]


def _fallback(text: str, title: str, flagged: bool) -> JDProfile:
    """Model unreachable: scan for known terms. Everything is 'nice' so nothing is auto-added."""
    found = [(t, mention(t, text)) for t in all_terms()]
    skills = [SkillNeed(t, v, "nice", weight(t, "nice", text, title)) for t, v in found if v]
    return JDProfile(
        role_title=title[:120],
        level=level_for("", title),
        years_min=verified_years(None, text),
        work_mode=work_mode(text),
        skills=skills,
        degraded=True,
        injection_flagged=flagged,
    )
