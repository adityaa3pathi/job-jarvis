"""Facts read from a job description, and the plain-code rules that check what a model says."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from jobagent.resume.ontology import canonical, mention

LEVELS = ("intern", "junior", "mid", "senior", "lead")
MAX_SKILLS = 25
IMPLICIT_WEIGHT = 0.3
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\b", re.I)
_TITLE_LEVELS = (
    ("lead", r"\b(?:lead|principal|staff|head)\b"),
    ("senior", r"\b(?:senior|sr\.?)\b"),
    ("junior", r"\b(?:junior|jr\.?|entry[- ]level|fresher|graduate|associate)\b"),
    ("intern", r"\bintern(?:ship)?\b"),
)


class PassSkill(BaseModel):
    name: str
    importance: str = "nice"

    @field_validator("importance")
    @classmethod
    def _known(cls, value: str) -> str:
        return "must" if value.strip().lower() in ("must", "required", "must_have") else "nice"


class Pass(BaseModel):
    role_title: str = ""
    level: str = "unknown"
    years_min: int | None = None
    domain: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    skills: list[PassSkill] = Field(default_factory=list)
    implicit_skills: list[str] = Field(default_factory=list)


class Critique(BaseModel):
    missing: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)


@dataclass
class SkillNeed:
    name: str
    verbatim: str
    importance: str  # must | nice
    weight: float
    implicit: bool = False


@dataclass
class JDProfile:
    role_title: str = ""
    level: str = "unknown"
    years_min: int | None = None
    work_mode: str = "unknown"
    domain: str = ""
    responsibilities: list[str] = field(default_factory=list)
    skills: list[SkillNeed] = field(default_factory=list)
    implicit_skills: list[SkillNeed] = field(default_factory=list)
    degraded: bool = False
    injection_flagged: bool = False
    rounds: int = 0

    @property
    def must_have(self) -> list[str]:
        return [s.name for s in self.skills if s.importance == "must"]

    @property
    def nice_to_have(self) -> list[str]:
        return [s.name for s in self.skills if s.importance == "nice"]

    @property
    def keywords_verbatim(self) -> list[str]:
        return list(dict.fromkeys(s.verbatim for s in self.skills))


def work_mode(text: str) -> str:
    lowered = text.lower()
    if "hybrid" in lowered:
        return "hybrid"
    if re.search(r"on[- ]?site|in[- ]office|work from office", lowered):
        return "onsite"
    return "remote" if "remote" in lowered else "unknown"


def level_for(claimed: str, title: str) -> str:
    for level, pattern in _TITLE_LEVELS:
        if re.search(pattern, title, re.I):
            return level
    value = claimed.strip().lower()
    return value if value in LEVELS else "unknown"


def verified_years(claimed: int | None, text: str) -> int | None:
    """A model-claimed years figure counts only if the posting really says that number."""
    stated = {int(m.group(1)) for m in _YEARS.finditer(text)}
    return claimed if claimed in stated else (min(stated) if stated else None)


def weight(name: str, importance: str, text: str, title: str) -> float:
    base = 1.0 if importance == "must" else 0.5
    forms = re.findall(re.escape(mention(name, text) or name), text, re.I)
    bonus = min(0.1 * max(len(forms) - 1, 0), 0.3)
    in_title = 0.3 if mention(name, title) else 0.0
    return round(min(base + bonus + in_title, 2.0), 2)


def vote(passes: list[Pass], text: str, title: str) -> list[SkillNeed]:
    """Keep a skill only if most passes found it and the posting really contains it."""
    seen: Counter[str] = Counter()
    musts: Counter[str] = Counter()
    for one in passes:
        names = {canonical(s.name): s.importance for s in one.skills}
        for name, importance in names.items():
            seen[name] += 1
            musts[name] += importance == "must"
    needs = []
    for name, votes in seen.items():
        verbatim = mention(name, text)
        if votes * 2 <= len(passes) or verbatim is None:
            continue
        importance = "must" if musts[name] * 2 > votes else "nice"
        needs.append(SkillNeed(name, verbatim, importance, weight(name, importance, text, title)))
    return sorted(needs, key=lambda n: (-n.weight, n.name))[:MAX_SKILLS]


def implicit_from(passes: list[Pass], text: str) -> list[SkillNeed]:
    """Skills a role usually needs but the posting never names. Prep list only, never the resume."""
    seen = Counter(canonical(n) for one in passes for n in set(one.implicit_skills))
    return [
        SkillNeed(name, name, "nice", IMPLICIT_WEIGHT, implicit=True)
        for name, votes in seen.items()
        if votes * 2 > len(passes) and mention(name, text) is None
    ][:10]


def fit_check(profile: JDProfile, years_have: float) -> list[str]:
    notes = []
    if profile.years_min is not None and profile.years_min > years_have + 1:
        notes.append(f"Posting asks for {profile.years_min}+ years; you have about {years_have:g}.")
    if profile.level in ("senior", "lead") and years_have < 3:
        notes.append(f"Posting is {profile.level} level; your experience is below that.")
    return notes
