"""Sorts each skill a job asks for into proven, adjacent or gap, using the evidence cards."""

from __future__ import annotations

from dataclasses import dataclass, field

from jobagent.resume.evidence import EvidenceCard
from jobagent.resume.jd import JDProfile, SkillNeed
from jobagent.resume.ontology import canonical, mention, related

ADJACENT_CREDIT = 0.5


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


def _judge(need: SkillNeed, cards: list[EvidenceCard]) -> MatchItem:
    name = canonical(need.name)
    exact = [c.id for c in cards if c.kind == "skill" and canonical(c.text) == name]
    said = [c.id for c in cards if c.kind != "skill" and mention(name, c.text)]
    if exact or said:
        return MatchItem(need, "proven", exact + said)
    near = [c for c in cards if c.kind == "skill" and related(name, c.text)]
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
