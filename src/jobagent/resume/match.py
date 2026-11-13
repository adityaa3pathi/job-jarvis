"""Sorts each skill a job asks for into proven, adjacent or gap, using the evidence cards."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobagent.resume.evidence import EvidenceCard
from jobagent.resume.jd import JDProfile, SkillNeed
from jobagent.resume.ontology import all_terms, canonical, mention, related

ADJACENT_CREDIT = 0.5
_SPLIT = re.compile(r"\s*(?:&|/|,|\+|\(|\)|\band\b)\s*", re.I)


@dataclass
class MatchItem:
    need: SkillNeed
    status: str  # proven | adjacent | gap
    card_ids: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class Coverage:
    items: list[MatchItem]
    score: float
    auto_add: list[str]
    prep: list[str]


def parts(name: str) -> list[str]:
    """A combined need and each skill inside it: "Embeddings & Vector Databases (pgvector)"
    is also "Embeddings", "Vector Databases" and "pgvector". Tiny parts only if known."""
    pieces = [p.strip() for p in _SPLIT.split(name)]
    found = [p for p in pieces if len(p) >= 3 or canonical(p) != p]
    known = [t for t in all_terms() if mention(t, name)]  # keeps "CI/CD" whole
    return list(dict.fromkeys([name.strip(), *known, *found]))


def _stem(word: str) -> str:
    return re.sub(r"(ations?|ions?|ing|ers?|ed|es|s)$", "", word.lower())


def _says(name: str, text: str) -> bool:
    """Named as written, or every word of it in some form: "Monitoring" in "uptime monitor"."""
    if mention(name, text):
        return True
    want = {_stem(w) for w in re.findall(r"[A-Za-z]{4,}", name)}
    have = {_stem(w) for w in re.findall(r"[A-Za-z]{3,}", text)}
    return bool(want) and want <= have


def _judge(need: SkillNeed, cards: list[EvidenceCard]) -> MatchItem:
    names = [canonical(p) for p in parts(need.name)]
    exact = [c.id for c in cards if c.kind == "skill" and canonical(c.text) in names]
    said = [c.id for c in cards if c.kind != "skill" and any(_says(n, c.text) for n in names)]
    if exact or said:
        return MatchItem(need, "proven", list(dict.fromkeys(exact + said)))
    near = [c for c in cards if c.kind == "skill" and any(related(n, c.text) for n in names)]
    if near:
        return MatchItem(need, "adjacent", [c.id for c in near], [c.text for c in near])
    return MatchItem(need, "gap")


def match_profile(profile: JDProfile, cards: list[EvidenceCard]) -> Coverage:
    items = [_judge(n, cards) for n in (*profile.skills, *profile.implicit_skills)]
    explicit = [i for i in items if not i.need.implicit]
    total = sum(i.need.weight for i in explicit)
    earned = sum(
        i.need.weight * (1.0 if i.status == "proven" else ADJACENT_CREDIT)
        for i in explicit
        if i.status != "gap"
    )
    return Coverage(
        items=items,
        score=round(earned / total, 2) if total else 0.0,
        auto_add=[
            i.need.name for i in explicit if i.status == "gap" and i.need.importance == "must"
        ],
        prep=[i.need.name for i in items if i.status != "proven"],
    )
