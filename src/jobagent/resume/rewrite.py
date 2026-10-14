"""Tailors a parsed resume to one job. Every model rewrite must pass plain-code checks first."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from jobagent.lexicon import KNOWN_TECH, find_numbers, find_scope, words
from jobagent.llm import LLM, LLMError, ask, data_block
from jobagent.resume.evidence import EvidenceCard
from jobagent.resume.jd import JDProfile
from jobagent.resume.latex import Command, Raw, Resume, Section, from_plain, to_plain
from jobagent.resume.match import Coverage
from jobagent.resume.ontology import all_terms, canonical, mention

MAX_SKILLS = 15
MAX_BULLET_CHARS = 320
BULLET_SECTIONS = ("Experience", "Projects")
_AI = {"RAG", "LLM", "MCP", "Generative AI", "LangGraph", "LangChain", "CrewAI", "pgvector",
       "FAISS", "Chroma", "Pinecone", "vLLM", "PEFT", "QLoRA", "Gemini API", "OpenAI API",
       "Claude API", "PyTorch", "Hugging Face Transformers", "bitsandbytes"}  # fmt: skip
_EVAL = {"RAGAS", "Langfuse", "LangSmith"}
_BACKEND = {"Python", "TypeScript", "JavaScript", "Go", "Java", "Rust", "C++", "C#", "Kotlin",
            "FastAPI", "Flask", "Django", "Node.js", "Express.js", "tRPC", "REST", "GraphQL",
            "Pydantic"}  # fmt: skip
CATEGORIES = (
    ("Languages \\& Backend", _BACKEND),
    ("AI \\& Retrieval", _AI),
    ("Eval \\& Observability", _EVAL),
)
OTHER = "Data, Infra \\& Frontend"
SYSTEM = (
    "Rewrite one resume bullet for a job. Keep every fact, number, tool and level of "
    "responsibility exactly as written; add none. Start with a strong action verb. You may use "
    "these job terms only where the bullet already supports them: {terms}. Mark up to 3 key "
    'terms with **double asterisks**. Reply as {{"text": str}}.'
)


class _Rewrite(BaseModel):
    text: str


@dataclass
class Change:
    kind: str  # bullet | reorder | skills
    before: str
    after: str
    reason: str


@dataclass
class TailorResult:
    latex: str
    changes: list[Change] = field(default_factory=list)
    added_gap_skills: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (bullet, model error)
    dropped_skills: list[str] = field(default_factory=list)


def check_rewrite(original: str, new: str, allowed: set[str]) -> str | None:
    """Returns the reason a rewrite is refused, or None when it adds nothing new."""
    if not new.strip():
        return "empty"
    if len(new) > min(MAX_BULLET_CHARS, max(int(len(original) * 1.25), len(original) + 40)):
        return "too long"
    if not set(find_numbers(new)) <= set(find_numbers(original)):
        return "new number"
    if not find_scope(new) <= find_scope(original):
        return "bigger role claimed"
    known = {t.lower() for t in allowed} | words(original)
    new_tech = {w for w in words(new) & KNOWN_TECH if w not in known}
    new_tech |= {
        t for t in all_terms() if mention(t, new) and not mention(t, original)
        and t.lower() not in known
    }  # fmt: skip
    return f"new tool: {sorted(new_tech)[0]}" if new_tech else None


def _runs(section: Section) -> list[list[int]]:
    """Positions of bullets that sit together in one list."""
    runs: list[list[int]] = []
    current: list[int] = []
    for i, node in enumerate(section.nodes):
        if isinstance(node, Command) and node.name == "resumeItem":
            current.append(i)
        elif not (isinstance(node, Raw) and not node.text.strip()):
            runs.append(current)
            current = []
    return [r for r in [*runs, current] if r]


def _score(plain: str, coverage: Coverage) -> float:
    return sum(
        i.need.weight for i in coverage.items
        if i.status == "proven" and not i.need.implicit and mention(i.need.name, plain)
    )  # fmt: skip


def _tailor_bullets(
    resume: Resume, cards: list[EvidenceCard], profile: JDProfile, coverage: Coverage,
    llm: LLM, out: TailorResult,
) -> None:  # fmt: skip
    allowed = {canonical(c.text) for c in cards if c.kind == "skill"}
    terms = ", ".join(
        i.need.verbatim for i in coverage.items if i.status == "proven" and not i.need.implicit
    )
    for title in BULLET_SECTIONS:
        try:
            section = resume.section(title)
        except KeyError:
            continue
        for run in _runs(section):
            for pos in run:
                cmd = section.nodes[pos]
                assert isinstance(cmd, Command)
                _rewrite_one(cmd, allowed, terms, profile, llm, out)
            order = sorted(
                (section.nodes[p] for p in run),
                key=lambda n: -_score(to_plain(n.args[0]) or "", coverage),  # type: ignore[union-attr]
            )
            if [id(n) for n in order] != [id(section.nodes[p]) for p in run]:
                out.changes.append(Change("reorder", title, title, "most relevant bullets first"))
                for pos, node in zip(run, order, strict=True):
                    section.nodes[pos] = node


def _rewrite_one(
    cmd: Command, allowed: set[str], terms: str, profile: JDProfile, llm: LLM, out: TailorResult
) -> None:
    plain = to_plain(cmd.args[0])
    if plain is None:
        return
    user = data_block("bullet", plain) + data_block("job_title", profile.role_title)
    try:
        new = ask(llm, _Rewrite, system=SYSTEM.format(terms=terms or "none"), user=user).text
    except LLMError as exc:
        out.failed.append((plain, str(exc)))
        return
    new = " ".join(new.split())
    if new == plain:
        return
    reason = check_rewrite(plain.replace("**", ""), new.replace("**", ""), allowed)
    if reason:
        out.rejected.append((plain, reason))
        return
    cmd.args[0] = from_plain(new)
    out.changes.append(Change("bullet", plain, new, "stronger wording for this job"))


def _bucket(skill: str) -> str:
    for label, members in CATEGORIES:
        if skill in members:
            return label
    return OTHER


def build_skills(
    cards: list[EvidenceCard], coverage: Coverage, cap: int = MAX_SKILLS, add_gaps: bool = True
) -> tuple[list[str], list[str]]:
    """The skills to show, best for this job first, and which of them are not backed by evidence."""
    proven = [i for i in coverage.items if i.status == "proven" and not i.need.implicit]
    shown = [i.need.verbatim for i in sorted(proven, key=lambda i: -i.need.weight)]
    gaps = []
    if add_gaps:
        verb = {i.need.name: i.need.verbatim for i in coverage.items}
        gaps = [verb.get(n, n) for n in coverage.auto_add]
    have = {canonical(s) for s in shown}
    extras = [c.text for c in cards if c.kind == "skill" and canonical(c.text) not in have]
    ordered = [*shown[: max(cap - len(gaps), 0)], *gaps, *extras]
    unique = list(dict.fromkeys(ordered))
    kept = unique[:cap]
    return kept, [g for g in gaps if g in kept]


def _listed_skills(section_tex: str) -> list[str]:
    """Skill names in a 'Label: a, b (c, d)' style Skills section."""
    names: list[str] = []
    for line in re.findall(r"\\textbf\{[^}]*:\}([^\n]*)", section_tex):
        line = line.replace("\\\\", "").strip()
        plain = to_plain(line) or re.sub(r"\\[a-zA-Z]+|[{}]", "", line)
        depth, part = 0, ""
        for ch in plain + ",":
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                names.append(part.strip())
                part = ""
            else:
                part += ch
    return [n for n in names if n]


def _dropped(before: list[str], cards: list[EvidenceCard], kept: list[str]) -> list[str]:
    """Owner skills (old section or evidence) that are not in the new section."""
    have = {canonical(s) for s in kept}
    owned = [*before, *(c.text for c in cards if c.kind == "skill")]
    out: dict[str, str] = {}
    for name in owned:
        key = canonical(name)
        if key not in have:
            out.setdefault(key, name)
    return list(out.values())


def _skills_block(skills: list[str]) -> str:
    groups: dict[str, list[str]] = {}
    for skill in skills:
        groups.setdefault(_bucket(canonical(skill)), []).append(skill)
    labels = [label for label, _ in CATEGORIES] + [OTHER]
    lines = [
        f"\\textbf{{{label}:}} {', '.join(from_plain(s) for s in groups[label])} \\\\"
        for label in labels
        if label in groups
    ]
    body = "\n\n".join(lines)
    return (
        "\n\\begin{itemize}[leftmargin=0.15in, label={}]\n"
        f"\\small{{\\item{{\n\n{body}\n\n}}}}\n\\end{{itemize}}\n\n"
    )


def tailor_resume(
    resume: Resume,
    cards: list[EvidenceCard],
    profile: JDProfile,
    coverage: Coverage,
    llm: LLM,
    add_gaps: bool = True,
) -> TailorResult:
    out = TailorResult(latex="")
    _tailor_bullets(resume, cards, profile, coverage, llm, out)
    section = next((s for s in resume.sections if re.search("skill", s.title, re.I)), None)
    if section is not None:
        skills, added = build_skills(cards, coverage, add_gaps=add_gaps)
        before = "".join(n.emit() for n in section.nodes)
        section.nodes[:] = [Raw(_skills_block(skills))]
        out.added_gap_skills = added
        out.dropped_skills = _dropped(_listed_skills(before), cards, skills)
        out.changes.append(
            Change("skills", before.strip(), ", ".join(skills), "ordered for this job")
        )
    out.latex = resume.emit()
    return out
