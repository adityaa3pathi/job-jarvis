"""Facts read from a job description, and the plain-code rules that check what a model says."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from jobagent.resume.ontology import canonical, known, mention, positions, says

LEVELS = ("intern", "junior", "mid", "senior", "lead")
MAX_SKILLS = 60  # a long posting names 40+; the old cap of 25 lost must-haves
IMPLICIT_WEIGHT = 0.3
# "3+ years", "1-6 yrs", "1 to 6+ Years", "1–6+ years": the first number is the minimum.
_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b", re.I
)
# Traits, not skills a profile can show: never listed, whatever the model returns.
_SOFT = re.compile(
    r"^(?:(?:strong |good |excellent )?(?:problem[- ]solving|communication|ownership|teamwork"
    r"|team player|leadership|collaboration|attention to detail|critical thinking|"
    r"time management|adaptability|analytical(?: thinking| skills)?|interpersonal|self[- ]starter"
    r"|work ethic|ownership mindset|growth mindset|curiosity)(?: skills)?)$",
    re.I,
)
# Interview topics a work profile cannot prove: prep list only, never a missing must-have.
_PREP = re.compile(
    r"^(?:dsa|data structures(?: (?:and|&) algorithms)?|algorithms|lld|hld|low[- ]level design"
    r"|high[- ]level design|competitive programming|leetcode|coding rounds?)$",
    re.I,
)
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
    said = len(positions(name, text))  # whole words: "SQL" inside "PostgreSQL" is not SQL
    bonus = min(0.1 * max(said - 1, 0), 0.3)
    in_title = 0.3 if mention(name, title) else 0.0
    return round(min(base + bonus + in_title, 2.0), 2)


_NICE_HEAD = re.compile(
    r"^(?:(?:good|nice)[ -]to[ -]have|preferred|bonus|(?:a |big )?plus|desirable|optional"
    r"|added advantage|additional|extra credit|it would be (?:great|nice))", re.I,
)  # fmt: skip
_MUST_HEAD = re.compile(
    r"^(?:required|requirements|must[ -]haves?|mandatory|minimum|basic|key skills|skills"
    r"|required skills|technical skills|qualifications|what you(?:'ll| will)? need"
    r"|what we(?:'re| are) looking for|who you are|you have|you are a great fit|ideal candidate"
    r"|eligibility)", re.I,
)  # fmt: skip
_INTERVIEW_HEAD = re.compile(
    r"^(?:(?:the )?(?:interview|selection|hiring|recruitment) (?:process|rounds?|stages?|steps?)"
    r"|interviews?|how (?:we|you(?:'ll| will)?) (?:hire|interview))\s*$", re.I,
)  # fmt: skip
_OTHER_HEAD = re.compile(
    r"^(?:key )?(?:responsibilities|duties)|^what you(?:'ll| will) do|^about|^(?:the )?role"
    r"|^(?:a )?day in the life|^benefits|^perks|^we offer|^why |^who we are|^job description"
    r"|^description|^overview|^location|^experience|^employment|^work mode|^salary|^compensation",
    re.I,
)  # fmt: skip
_NICE_CUE = re.compile(
    r"\b(?:(?:nice|good)[ -]to[ -]have|preferred|preferably|plus|bonus|advantage(?:ous)?"
    r"|desirable|optional)\b", re.I,
)  # fmt: skip
_MUST_CUE = re.compile(r"\b(?:required|must|mandatory|essential)\b", re.I)
_CLAUSE = re.compile(r"[^.;!?\n]+(?:[.;!?](?=\s|$))?")
_CHOICES = re.compile(r"\s*(?:/|,|\bor\b)\s*", re.I)


def _heading(line: str) -> str | None:
    """'must', 'nice', 'interview' or 'other' when the line opens a part of the posting."""
    bare = re.sub(r"^[\s#*•·\-–>]+|[*#]+$", "", line).strip()
    head = bare.split(":", 1)[0] if ":" in bare[:50] else bare
    if not head or len(head) > 60:
        return None
    heads = (("interview", _INTERVIEW_HEAD), ("nice", _NICE_HEAD), ("must", _MUST_HEAD),
             ("other", _OTHER_HEAD))  # fmt: skip
    for label, pattern in heads:
        if pattern.match(head):
            return label
    return None


def _parts_of(text: str) -> list[tuple[int, int, str | None]]:
    """(start, end, label) per line: the label of the part of the posting it sits in."""
    out, label, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        found = _heading(line)
        if found is not None:
            label = None if found == "other" else found
        out.append((pos, pos + len(line), label))
        pos += len(line)
    return out


def importance_in(text: str, at: list[int], claimed: str) -> str:
    """Must or nice as the posting itself puts it: a "required" or "a plus" in the same
    sentence first, then the heading the skill sits under, then what the model said."""
    labels = []
    for start, end, label in _parts_of(text):
        line = text[start:end]
        for pos in (p - start for p in at if start <= p < end):
            clause = next(
                (m.group(0) for m in _CLAUSE.finditer(line) if m.start() <= pos < m.end()), line
            )
            nice, must = bool(_NICE_CUE.search(clause)), bool(_MUST_CUE.search(clause))
            part = None if label == "interview" else label
            labels.append("nice" if nice and not must else "must" if must and not nice else part)
    if "must" in labels:
        return "must"
    return "nice" if "nice" in labels else claimed


def only_in_interview(text: str, at: list[int]) -> bool:
    """True when every mention sits in the interview-process part ("Coding round: DSA")."""
    labels = [label for start, end, label in _parts_of(text) for p in at if start <= p < end]
    return bool(labels) and all(label == "interview" for label in labels)


def locate(name: str, text: str) -> str | None:
    """How the posting spells this skill; the model's spelling when the posting says it in
    another form ("Fine-tuning" for "fine-tune"); None when the posting never says it."""
    if found := mention(name, text):
        return found
    return name.strip() if says(name, text) else None


def _named(name: str, text: str) -> list[str]:
    """Each choice in a list like "PyTorch or TensorFlow" or "AWS/GCP" when the posting names
    every one; a known compound such as CI/CD, or anything else, stays whole."""
    if known(name) or not _CHOICES.search(name):
        return [name]
    parts = [p.strip() for p in _CHOICES.split(name) if len(p.strip()) >= 2]
    if len(parts) > 1 and all(mention(p, text) for p in parts):
        return parts
    return [name] if locate(name, text) else [p for p in parts if locate(p, text)]


def vote(passes: list[Pass], text: str, title: str) -> list[SkillNeed]:
    """Keep a skill only if most passes found it and the posting really contains it."""
    seen: Counter[str] = Counter()
    musts: Counter[str] = Counter()
    for one in passes:
        names = {canonical(part): s.importance for s in one.skills for part in _named(s.name, text)}
        for name, importance in names.items():
            seen[name] += 1
            musts[name] += importance == "must"
    needs = []
    for name, votes in seen.items():
        verbatim = locate(name, text)
        if votes * 2 <= len(passes) or verbatim is None or _SOFT.match(name.strip()):
            continue
        at = positions(name, text)
        if _PREP.match(name.strip()) or only_in_interview(text, at):
            needs.append((SkillNeed(name, verbatim, "nice", IMPLICIT_WEIGHT, implicit=True), 10**9))
            continue
        claimed = "must" if musts[name] * 2 > votes else "nice"
        importance = importance_in(text, at, claimed)
        need = SkillNeed(name, verbatim, importance, weight(name, importance, text, title))
        needs.append((need, at[0] if at else len(text)))
    needs.sort(key=lambda n: (-n[0].weight, n[1]))
    return [need for need, _ in needs][:MAX_SKILLS]


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
