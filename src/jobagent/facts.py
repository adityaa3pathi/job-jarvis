"""Loads and validates the fact sheet, the only source of claims (R5, KTD5)."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from jobagent.lexicon import Vocabulary, blank_spans, canon_number, find_numbers, find_scope

REQUIRED_ANSWERS = ("full_name", "email")
Track = Literal["ai", "full_stack"]


class FactsError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class Answer(BaseModel):
    id: str
    key: str
    question_patterns: list[str] = Field(default_factory=list)
    value: str
    allowed_values: list[str] = Field(default_factory=list)
    sensitive: bool = False
    confirmed: bool = False
    source: str = ""


class Role(BaseModel):
    id: str
    employer: str
    descriptor: str = ""
    titles: list[str]
    start: str
    end: str
    location: str = ""
    source: str = ""
    confirmed: bool = False


class Education(BaseModel):
    id: str
    degree: str
    institution: str
    start_year: int
    end_year: int
    source: str = ""
    confirmed: bool = False


class Project(BaseModel):
    id: str
    names: list[str]
    url: str = ""
    tech: list[str] = Field(default_factory=list)
    source: str = ""
    confirmed: bool = False


class Number(BaseModel):
    id: str
    values: list[str]
    meaning: str = ""
    source: str = ""
    confirmed: bool = False


class Bullet(BaseModel):
    id: str
    text: str
    parent: str | None = None
    kind: Literal["bullet", "summary"] = "bullet"
    verbs: list[str] = Field(default_factory=list)
    scope_words: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    fallback: str | None = None
    source: str = ""
    confirmed: bool = False


class Skill(BaseModel):
    id: str
    canonical: str
    aliases: list[str]
    case_sensitive: bool = False
    evidence: list[str] = Field(default_factory=list)
    confirmed: bool = False


class BaseExperience(BaseModel):
    role: str
    title: str
    bullets: list[str]


class BaseProject(BaseModel):
    project: str
    name: str
    bullets: list[str]


class SkillLine(BaseModel):
    label: str
    skills: list[str]


class BaseResume(BaseModel):
    summary: str
    experience: list[BaseExperience]
    projects: list[BaseProject]
    skills: list[SkillLine]
    education: list[str]


class FactsFile(BaseModel):
    version: int = 1
    answers: list[Answer] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    numbers: list[Number] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    base_resumes: dict[Track, BaseResume] = Field(default_factory=dict)


class FactSheet:
    def __init__(self, data: FactsFile, raw_hash: str = "") -> None:
        self.data = data
        self.hash = raw_hash
        self.answers = {a.id: a for a in data.answers}
        self.roles = {r.id: r for r in data.roles}
        self.education = {e.id: e for e in data.education}
        self.projects = {p.id: p for p in data.projects}
        self.numbers = {n.id: n for n in data.numbers}
        self.bullets = {b.id: b for b in data.bullets}
        self.skills = {s.id: s for s in data.skills}
        self.vocab = Vocabulary([(s.id, s.aliases, s.case_sensitive) for s in data.skills])

    # --- lookups ---
    def exists(self, fact_id: str) -> bool:
        return (
            any(
                fact_id in table
                for table in (self.answers, self.roles, self.education, self.projects)
            )
            or fact_id in self.numbers
            or fact_id in self.bullets
            or fact_id in self.skills
        )

    def is_confirmed(self, fact_id: str) -> bool:
        for table in (
            self.answers, self.roles, self.education, self.projects,
            self.numbers, self.bullets, self.skills,
        ):  # fmt: skip
            if fact_id in table:
                return bool(table[fact_id].confirmed)
        return False

    def bullet_skill_ids(self, bullet_id: str) -> set[str]:
        """Skills named in the bullet text plus the tech of its parent project."""
        bullet = self.bullets[bullet_id]
        found = {h.id for h in self.vocab.find(bullet.text)}
        if bullet.parent in self.projects:
            found |= set(self.projects[bullet.parent].tech)
        return found

    def resolve_bullet(self, bullet_id: str) -> Bullet | None:
        """The bullet if confirmed, else its confirmed fallback, else None."""
        bullet = self.bullets[bullet_id]
        if bullet.confirmed:
            return bullet
        if bullet.fallback and self.bullets[bullet.fallback].confirmed:
            return self.bullets[bullet.fallback]
        return None

    def usable_numbers(self) -> dict[str, str]:
        return {
            canon_number(v): n.id for n in self.numbers.values() if n.confirmed for v in n.values
        }

    def confirmed_answer(self, key: str) -> Answer | None:
        for ans in self.answers.values():
            if ans.key == key and ans.confirmed:
                return ans
        return None

    def experience_years(self, today: date, rounding: str = "down") -> int:
        """Whole years from the earliest role start to today (or the latest end)."""
        starts, ends = [], []
        for role in self.roles.values():
            y, m = (int(x) for x in role.start.split("-"))
            starts.append((y, m))
            if role.end == "present":
                ends.append((today.year, today.month))
            else:
                ey, em = (int(x) for x in role.end.split("-"))
                ends.append((ey, em))
        if not starts:
            return 0
        (sy, sm), (ey, em) = min(starts), max(ends)
        months = max(0, (ey - sy) * 12 + (em - sm))
        years = months / 12
        if rounding == "up":
            return int(-(-months // 12))
        if rounding == "nearest":
            return int(years + 0.5)
        return int(years)

    def skill_years(self, skill_id: str, today: date) -> int:
        """Whole years from dated roles whose confirmed bullets use the skill. Undated work is 0."""
        spans = []
        for b in self.bullets.values():
            if b.confirmed and b.parent in self.roles and skill_id in self.bullet_skill_ids(b.id):
                role = self.roles[b.parent]
                spans.append((_ym(role.start), _ym(role.end, today)))
        if not spans:
            return 0
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        return max(0, end - start) // 12

    def llm_view(self, fact_ids: list[str]) -> list[dict[str, str]]:
        """Fact text safe to send to a model: never answers, phone, email or address."""
        out = []
        for fid in fact_ids:
            if fid in self.bullets:
                out.append({"id": fid, "text": self.bullets[fid].text})
            elif fid in self.skills:
                out.append({"id": fid, "text": self.skills[fid].canonical})
        return out


def _ym(value: str, today: date | None = None) -> int:
    """Months since year 0 for 'YYYY-MM' or 'present'."""
    if value == "present":
        assert today is not None
        return today.year * 12 + today.month
    year, month = (int(x) for x in value.split("-"))
    return year * 12 + month


def validate_answer(answer: Answer) -> list[str]:
    problems = []
    if answer.allowed_values and answer.value not in answer.allowed_values:
        problems.append(f"answer {answer.id}: value not in allowed_values")
    if answer.confirmed and answer.question_patterns == [] and answer.allowed_values:
        problems.append(f"answer {answer.id}: allowed_values need a question pattern")
    return problems


def _evidence_problems(sheet: FactSheet) -> list[str]:
    problems = []
    for skill in sheet.skills.values():
        if not skill.evidence:
            problems.append(f"skill {skill.id}: no evidence")
            continue
        good = False
        for ref in skill.evidence:
            if ref.startswith("resume:"):
                good = True
            elif not sheet.exists(ref):
                problems.append(f"skill {skill.id}: evidence {ref!r} not found")
            elif sheet.is_confirmed(ref):
                good = True
        if skill.confirmed and not good:
            problems.append(f"skill {skill.id}: confirmed without confirmed evidence")
    return problems


def _bullet_problems(sheet: FactSheet) -> list[str]:
    problems = []
    for b in sheet.bullets.values():
        if b.parent and not (b.parent in sheet.roles or b.parent in sheet.projects):
            problems.append(f"bullet {b.id}: unknown parent {b.parent}")
        for nid in b.numbers:
            if nid not in sheet.numbers:
                problems.append(f"bullet {b.id}: unknown number {nid}")
        if b.fallback and b.fallback not in sheet.bullets:
            problems.append(f"bullet {b.id}: unknown fallback {b.fallback}")
        hits = sheet.vocab.find(b.text)
        listed = {
            canon_number(v)
            for n in b.numbers
            if n in sheet.numbers
            for v in sheet.numbers[n].values
        }
        for token in find_numbers(blank_spans(b.text, hits)):
            if token not in listed:
                problems.append(f"bullet {b.id}: number {token!r} in text has no number fact")
        missing = find_scope(b.text) - set(b.scope_words)
        if missing:
            problems.append(f"bullet {b.id}: scope words {sorted(missing)} not declared")
        if not b.confirmed:
            continue
        for h in hits:
            if not sheet.skills[h.id].confirmed:
                problems.append(f"bullet {b.id}: confirmed but uses unconfirmed skill {h.id}")
        in_text = set(find_numbers(blank_spans(b.text, hits)))
        for nid in b.numbers:
            num = sheet.numbers.get(nid)
            if num and not num.confirmed and in_text & {canon_number(v) for v in num.values}:
                problems.append(f"bullet {b.id}: confirmed but contains unconfirmed number {nid}")
        if b.parent and not sheet.is_confirmed(b.parent):
            problems.append(f"bullet {b.id}: confirmed but parent {b.parent} is not")
    return problems


def _base_problems(sheet: FactSheet) -> list[str]:
    problems = []
    for track, base in sheet.data.base_resumes.items():
        if base.summary not in sheet.bullets:
            problems.append(f"base {track}: unknown summary {base.summary}")
        for exp in base.experience:
            if exp.role not in sheet.roles or exp.title not in sheet.roles[exp.role].titles:
                problems.append(f"base {track}: bad role/title {exp.role}/{exp.title}")
            problems += [
                f"base {track}: unknown bullet {b}" for b in exp.bullets if b not in sheet.bullets
            ]
        for proj in base.projects:
            if (
                proj.project not in sheet.projects
                or proj.name not in sheet.projects[proj.project].names
            ):
                problems.append(f"base {track}: bad project/name {proj.project}/{proj.name}")
            problems += [
                f"base {track}: unknown bullet {b}" for b in proj.bullets if b not in sheet.bullets
            ]
        for line in base.skills:
            problems += [
                f"base {track}: unknown skill {s}" for s in line.skills if s not in sheet.skills
            ]
        problems += [
            f"base {track}: unknown education {e}"
            for e in base.education
            if e not in sheet.education
        ]
    return problems


def check_sheet(data: FactsFile, raw_hash: str = "") -> FactSheet:
    problems: list[str] = []
    ids: list[str] = []
    for table in (
        data.answers, data.roles, data.education, data.projects, data.numbers, data.bullets,
        data.skills,
    ):  # fmt: skip
        ids += [item.id for item in table]
    problems += [f"duplicate id {i}" for i in {x for x in ids if ids.count(x) > 1}]
    seen_alias: dict[str, str] = {}
    for skill in data.skills:
        for alias in skill.aliases:
            owner = seen_alias.setdefault(alias.lower(), skill.id)
            if owner != skill.id:
                problems.append(f"alias {alias!r} used by {owner} and {skill.id}")
    keys = {a.key for a in data.answers}
    problems += [f"missing required answer: {k}" for k in REQUIRED_ANSWERS if k not in keys]
    for ans in data.answers:
        problems += validate_answer(ans)
    sheet = FactSheet(data, raw_hash)
    if not problems:
        problems += _evidence_problems(sheet) + _bullet_problems(sheet) + _base_problems(sheet)
        for p in data.projects:
            problems += [
                f"project {p.id}: unknown tech {t}" for t in p.tech if t not in sheet.skills
            ]
    if problems:
        raise FactsError(problems)
    return sheet


def load_facts(path: Path) -> FactSheet:
    text = path.read_text()
    try:
        data = FactsFile.model_validate(yaml.safe_load(text) or {})
    except ValidationError as exc:
        raise FactsError([str(exc)]) from exc
    return check_sheet(data, hashlib.sha256(text.encode()).hexdigest())
