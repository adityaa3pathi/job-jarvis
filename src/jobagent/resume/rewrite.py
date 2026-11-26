"""Tailors a parsed resume to one job. Every model rewrite must pass plain-code checks first."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from pydantic import BaseModel, Field

from jobagent.lexicon import KNOWN_TECH, find_numbers, find_scope, words
from jobagent.llm import LLM, LLMError, ask, data_block, parallel
from jobagent.resume.evidence import EvidenceCard
from jobagent.resume.jd import JDProfile, SkillNeed
from jobagent.resume.latex import (
    Command,
    Raw,
    Resume,
    Section,
    from_plain,
    listed_skills,
    skills_section,
    summary_span,
    to_plain,
)
from jobagent.resume.match import Coverage
from jobagent.resume.ontology import all_terms, canonical, known, mention

MAX_SKILLS = 36  # every job skill the owner has first; was 15, which cut proven must-haves
MAX_BULLET_CHARS = 320
BULLET_SECTIONS = ("Experience", "Projects")
_LANGUAGES = {"python", "typescript", "javascript", "go", "java", "rust", "c++", "c#", "kotlin",
              "sql", "bash", "scala", "ruby", "php", "swift", "r", "c"}  # fmt: skip
_AI_WORDS = (
    "ai", "llm", "llms", "ml", "nlp", "rag", "genai", "gpt", "machine learning", "deep learning",
    "computer vision", "ocr", "generative", "agent", "agents", "agentic", "embedding",
    "embeddings", "vector", "fine-tuning", "fine tuning", "lora", "qlora", "peft", "prompt",
    "transformers", "hugging face", "pytorch", "tensorflow", "keras", "scikit-learn",
    "langchain", "langgraph", "llamaindex", "crewai", "autogen", "vllm", "openai", "gemini",
    "claude", "anthropic", "mcp", "pgvector", "faiss", "chroma", "pinecone", "weaviate",
    "milvus", "qdrant", "ragas", "langfuse", "langsmith", "mlflow", "mlops", "weights & biases",
    "model", "models", "retrieval", "reranking", "evaluation", "evals", "bitsandbytes",
    "inference",
)  # fmt: skip
_WEB_WORDS = (
    "fastapi", "flask", "django", "node.js", "express.js", "trpc", "rest", "restful", "api",
    "apis", "graphql", "grpc", "pydantic", "next.js", "react", "vue.js", "angular", "tailwind",
    "microservices", "spring boot", "spring", "html", "css", "websockets", "vercel ai sdk",
)  # fmt: skip
_INFRA_WORDS = (
    "postgresql", "mysql", "mongodb", "redis", "sqlite", "nosql", "sqlalchemy", "prisma",
    "database", "databases", "kafka", "spark", "hadoop", "airflow", "rabbitmq", "celery",
    "inngest", "elasticsearch", "snowflake", "dbt", "pandas", "numpy", "etl", "aws", "azure",
    "google cloud", "gcp", "docker", "kubernetes", "k8s", "ci/cd", "terraform", "linux", "git",
    "github actions", "jenkins", "helm", "ec2", "s3", "lambda", "gke", "eks", "cloud", "devops",
    "nginx", "serverless",
)  # fmt: skip
CATEGORIES = (
    ("Languages", None),
    ("AI \\& ML", _AI_WORDS),
    ("Backend \\& Web", _WEB_WORDS),
    ("Data, Cloud \\& DevOps", _INFRA_WORDS),
)
OTHER = "Tools \\& Practices"
_SMALL_WORDS = {"and", "or", "of", "for", "with", "the", "to", "in", "on", "a", "an"}
MAX_BATCH = 30  # bullets per model call; keeps the reply well under its token cap
SYSTEM = (
    "Rewrite each resume bullet for a job. Keep every fact, number, tool and level of "
    "responsibility exactly as written; add none. Start with a strong action verb. You may use "
    "these job terms only where a bullet already supports them: {terms}. Mark up to 3 key "
    "terms per bullet with **double asterisks**. Return every bullet once, by its id. "
    'Reply as {{"items": [{{"id": int, "text": str}}]}}.'
)
OWNER_STYLE = (
    " The owner's wishes for this resume are in the owner_style block. Follow them for wording, "
    "emphasis and tone only; every rule above still wins."
)
SUMMARY_SYSTEM = (
    "Rewrite the summary at the top of a resume for one job. Keep every fact, number, tool "
    "and level of responsibility as written; add none. Open with the candidate's own role, "
    "then lead with what this job asks for most. You may use these job terms, which the "
    "candidate's evidence backs: {terms}. Keep **double asterisks** around key numbers. "
    'No longer than the original. Reply as {{"text": str}}.'
)
MAX_INSTRUCTIONS = 1000


class _Item(BaseModel):
    id: int
    text: str


class _Batch(BaseModel):
    items: list[_Item] = Field(default_factory=list)


class _Summary(BaseModel):
    text: str = ""


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
    summary: Change | None = None  # the tailored summary, when one was written and passed
    summary_note: str = ""  # why the summary was kept as it was


def check_rewrite(original: str, new: str, allowed: set[str]) -> str | None:
    """Returns the reason a rewrite is refused, or None when it adds nothing new."""
    if not new.strip():
        return "empty"
    grown = min(MAX_BULLET_CHARS, max(int(len(original) * 1.25), len(original) + 40))
    if len(new) > max(grown, len(original)):  # a bullet already past the cap may still tighten
        return "too long"
    return unsupported(original, new, allowed)


def unsupported(source: str, new: str, allowed: set[str]) -> str | None:
    """Why `new` claims more than `source` backs: a number, a bigger role or a tool."""
    if not set(find_numbers(new)) <= set(find_numbers(source)):
        return "new number"
    if not find_scope(new) <= find_scope(source):
        return "bigger role claimed"
    tools = new_tools(source, new, allowed)
    return f"new tool: {tools[0]}" if tools else None


def new_tools(source: str, new: str, allowed: set[str]) -> list[str]:
    known = {t.lower() for t in allowed} | words(source)
    found = {w for w in words(new) & KNOWN_TECH if w not in known}
    found |= {
        t for t in all_terms() if mention(t, new) and not mention(t, source)
        and t.lower() not in known
    }  # fmt: skip
    return sorted(found)


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


def _bullet_sections(resume: Resume) -> list[tuple[str, Section]]:
    sections = []
    for title in BULLET_SECTIONS:
        try:
            sections.append((title, resume.section(title)))
        except KeyError:
            continue
    return sections


def _bullet_requests(
    resume: Resume, coverage: Coverage, profile: JDProfile, instructions: str = ""
) -> list[_BatchRequest]:
    """One request per batch of bullets; the model is not called here."""
    terms = ", ".join(
        i.need.verbatim for i in coverage.items if i.status == "proven" and not i.need.implicit
    )
    sections = _bullet_sections(resume)
    cmds = [
        node for _, section in sections for run in _runs(section) for pos in run
        if isinstance(node := section.nodes[pos], Command)
    ]  # fmt: skip
    batches = (cmds[start : start + MAX_BATCH] for start in range(0, len(cmds), MAX_BATCH))
    return [r for batch in batches if (r := _batch_request(batch, terms, profile, instructions))]


def _reorder_bullets(resume: Resume, coverage: Coverage, out: TailorResult) -> None:
    for title, section in _bullet_sections(resume):
        for run in _runs(section):
            order = sorted(
                (section.nodes[p] for p in run),
                key=lambda n: -_score(to_plain(n.args[0]) or "", coverage),  # type: ignore[union-attr]
            )
            if [id(n) for n in order] != [id(section.nodes[p]) for p in run]:
                out.changes.append(Change("reorder", title, title, "most relevant bullets first"))
                for pos, node in zip(run, order, strict=True):
                    section.nodes[pos] = node


@dataclass
class _BatchRequest:
    cmds: list[Command]
    plains: dict[int, str]
    system: str
    user: str


def _batch_request(
    cmds: list[Command], terms: str, profile: JDProfile, instructions: str = ""
) -> _BatchRequest | None:
    """One model call for many bullets; each reply still passes `check_rewrite` on its own."""
    plains = {i: p for i, cmd in enumerate(cmds) if (p := to_plain(cmd.args[0])) is not None}
    if not plains:
        return None
    listed = "\n".join(f"{i}. {p}" for i, p in plains.items())
    user = data_block("job_title", profile.role_title) + data_block("bullets", listed)
    system = SYSTEM.format(terms=terms or "none")
    if instructions := instructions.strip()[:MAX_INSTRUCTIONS]:
        user += data_block("owner_style", instructions)
        system += OWNER_STYLE
    return _BatchRequest(cmds, plains, system, user)


def _apply_batch(
    req: _BatchRequest, batch: _Batch | LLMError, allowed: set[str], out: TailorResult
) -> None:
    if isinstance(batch, LLMError):
        out.failed += [(p, str(batch)) for p in req.plains.values()]
        return
    replies = {item.id: item.text for item in batch.items}
    for i, plain in req.plains.items():
        if i in replies:
            _apply(req.cmds[i], plain, replies[i], allowed, out)
        else:
            out.failed.append((plain, "model reply left this bullet out"))


def _apply(cmd: Command, plain: str, new: str, allowed: set[str], out: TailorResult) -> None:
    new = " ".join(new.split())
    if new == plain:
        return
    reason = check_rewrite(plain.replace("**", ""), new.replace("**", ""), allowed)
    if reason:
        out.rejected.append((plain, reason))
        return
    cmd.args[0] = from_plain(new)
    out.changes.append(Change("bullet", plain, new, "stronger wording for this job"))


def _ask_or_error[T: BaseModel](
    llm: LLM, schema: type[T], system: str, user: str, step: str
) -> T | LLMError:
    try:
        return ask(llm, schema, system=system, user=user, step=step)
    except LLMError as exc:
        return exc


def _allowed(cards: list[EvidenceCard], coverage: Coverage) -> set[str]:
    """Tools a rewrite may name: the owner's skills and the job skills their evidence proves."""
    owned = {canonical(c.text) for c in cards if c.kind == "skill"}
    return owned | {canonical(i.need.name) for i in coverage.items if i.status == "proven"}


def _bucket(skill: str) -> str:
    lower = canonical(skill).lower()
    if lower in _LANGUAGES or skill.lower() in _LANGUAGES:
        return CATEGORIES[0][0]
    for label, keys in CATEGORIES[1:]:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", lower) for k in keys or ()):
            return label
    return OTHER


def _title_case(text: str) -> str:
    return " ".join(
        w if w in _SMALL_WORDS or len(w) <= 2 else w[0].upper() + w[1:] for w in text.split()
    )


def shown_name(need: SkillNeed) -> str:
    """How a job skill reads in Skills: a known skill by its usual name, any other in the
    posting's own words (what an ATS looks for), capitalised when the posting wrote it in
    running text ("vector databases")."""
    said = need.verbatim
    if known(need.name) or need.name.lower() == said.lower():
        return need.name
    return _title_case(said) if said == said.lower() else said


def _covers(name: str, shown: list[str]) -> bool:
    """True when `name` is already in the list, as itself or inside a longer entry."""
    key = canonical(name).lower()
    return any(canonical(s).lower() == key or mention(name, s) for s in shown)


def build_skills(
    cards: list[EvidenceCard],
    coverage: Coverage,
    cap: int = MAX_SKILLS,
    add_gaps: bool = True,
    listed: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """The Skills to show and which of them no evidence backs. Order: every job skill the
    owner's evidence proves (must-haves first, then by weight and the posting's order), then
    must-have gaps if asked for, then the owner's own skills as their resume lists them."""
    proven = [i for i in coverage.items if i.status == "proven" and not i.need.implicit]
    proven.sort(key=lambda i: (i.need.importance != "must", -i.need.weight))
    needs = {i.need.name: i.need for i in coverage.items}
    gaps = (
        [shown_name(needs[n]) if n in needs else n for n in coverage.auto_add] if add_gaps else []
    )
    owned = [*(listed or []), *(c.text for c in cards if c.kind == "skill")]
    kept: list[str] = []
    for name in [*(shown_name(i.need) for i in proven), *gaps, *owned]:
        if len(kept) < cap and not _covers(name, kept):
            kept.append(name)
    return kept, [g for g in gaps if g in kept]


def _dropped(before: list[str], cards: list[EvidenceCard], kept: list[str]) -> list[str]:
    """Owner skills (old section or evidence) that the new section leaves out."""
    owned = [*before, *(c.text for c in cards if c.kind == "skill")]
    out: dict[str, str] = {}
    for name in owned:
        if not _covers(name, kept):
            out.setdefault(canonical(name).lower(), name)
    return list(out.values())


def _skills_block(skills: list[str]) -> str:
    groups: dict[str, list[str]] = {}
    for skill in skills:
        groups.setdefault(_bucket(skill), []).append(skill)
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


@dataclass
class _SummaryRequest:
    node: Raw
    start: int
    end: int
    before: str
    system: str
    user: str


def _summary_request(
    resume: Resume, coverage: Coverage, profile: JDProfile, out: TailorResult,
    instructions: str = "",
) -> _SummaryRequest | None:  # fmt: skip
    """Rewrite the top summary for this job under the same fact check as the bullets."""
    span = summary_span(resume)
    if span is None:
        out.summary_note = "no summary paragraph found at the top of the resume"
        return None
    node, start, end = span
    before = to_plain(node.text[start:end]) or ""
    proven = [i for i in coverage.items if i.status == "proven" and not i.need.implicit]
    proven.sort(key=lambda i: (i.need.importance != "must", -i.need.weight))
    terms = ", ".join(shown_name(i.need) for i in proven[:15]) or "none"
    user = data_block("job_title", profile.role_title) + data_block("summary", before)
    system = SUMMARY_SYSTEM.format(terms=terms)
    if instructions := instructions.strip()[:MAX_INSTRUCTIONS]:
        user += data_block("owner_style", instructions)
        system += OWNER_STYLE
    return _SummaryRequest(node, start, end, before, system, user)


def _apply_summary(
    req: _SummaryRequest, reply: _Summary | LLMError, cards: list[EvidenceCard],
    coverage: Coverage, out: TailorResult,
) -> None:  # fmt: skip
    if isinstance(reply, LLMError):
        out.summary_note = f"kept as it was: model error ({reply})"
        return
    node, start, end, before = req.node, req.start, req.end, req.before
    new = " ".join(reply.text.split())
    plain_before, plain_new = before.replace("**", ""), new.replace("**", "")
    if not plain_new or plain_new == plain_before:
        out.summary_note = "the model kept it as it was"
        return
    if len(plain_new) > len(plain_before) * 1.1 + 20:
        reason: str | None = "too long"
    else:
        reason = unsupported(plain_before, plain_new, _allowed(cards, coverage))
    if reason:
        out.summary_note = f"rewrite refused by the fact check ({reason}); kept as it was"
        out.rejected.append((before, f"summary: {reason}"))
        return
    lead = node.text[start:end][: len(node.text[start:end]) - len(node.text[start:end].lstrip())]
    trail = node.text[start:end][len(node.text[start:end].rstrip()) :]
    node.text = node.text[:start] + lead + from_plain(new) + trail + node.text[end:]
    out.summary = Change("summary", before, new, "summary tailored to the job")


def tailor_resume(
    resume: Resume,
    cards: list[EvidenceCard],
    profile: JDProfile,
    coverage: Coverage,
    llm: LLM,
    add_gaps: bool = True,
    instructions: str = "",
) -> TailorResult:
    out = TailorResult(latex="")
    summary = _summary_request(resume, coverage, profile, out, instructions)
    batches = _bullet_requests(resume, coverage, profile, instructions)
    # The summary and every bullet batch are independent, so the model writes them at the
    # same time; replies are then checked and applied in the usual order.
    calls: list[Callable[[], Any]] = [
        partial(_ask_or_error, llm, _Batch, r.system, r.user, "rewrite_bullets") for r in batches
    ]
    if summary is not None:
        calls.insert(0, partial(_ask_or_error, llm, _Summary, summary.system, summary.user,
                                "rewrite_summary"))  # fmt: skip
    replies = parallel(*calls)
    if summary is not None:
        _apply_summary(summary, replies.pop(0), cards, coverage, out)
    allowed = _allowed(cards, coverage)
    for req, batch in zip(batches, replies, strict=True):
        _apply_batch(req, batch, allowed, out)
    _reorder_bullets(resume, coverage, out)
    section = skills_section(resume)
    if section is not None:
        before = "".join(n.emit() for n in section.nodes)
        listed = listed_skills(before)
        skills, added = build_skills(cards, coverage, add_gaps=add_gaps, listed=listed)
        section.nodes[:] = [Raw(_skills_block(skills))]
        out.added_gap_skills = added
        out.dropped_skills = _dropped(listed, cards, skills)
        out.changes.append(
            Change("skills", before.strip(), ", ".join(skills), "ordered for this job")
        )
    out.latex = resume.emit()
    return out
