"""Builds a per-job resume from fact IDs only, then gates the exact files (R6, R7, KTD5)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from jobagent.facts import FactSheet, Track
from jobagent.llm import LLM, LLMError, ask, data_block
from jobagent.render import (
    Gap,
    ResumeBullet,
    ResumeEntry,
    SkillLineOut,
    TailoredResume,
    body_markdown,
    extract_pdf_text,
    gaps_markdown,
    strip_contact,
    to_markdown,
)
from jobagent.requirements import Requirements, extract_requirements
from jobagent.sanitize import clean_posting_text
from jobagent.truth import ApprovalLedger, CheckReport, TruthChecker, Violation, rule_violations

REWRITE_SYSTEM = (
    "Reword one resume line. Use only the tools, numbers and words already in the line. You "
    "may reorder, shorten or change tense. Never add a tool, number, role word or claim. "
    'Reply {"text": "..."}.'
)


class Rewrite(BaseModel):
    text: str


@dataclass
class TailorResult:
    resume: TailoredResume
    markdown: str
    gaps: list[Gap]
    gaps_md: str
    cover_note: str | None
    report: CheckReport
    injection_flagged: bool = False
    rejected_rewrites: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return not self.report.blocked


def _relevance(sheet: FactSheet, bullet_id: str, wanted: set[str]) -> int:
    return len({h.id for h in sheet.vocab.find(sheet.bullets[bullet_id].text)} & wanted)


def mirror_aliases(sheet: FactSheet, text: str, surfaces: dict[str, str]) -> str:
    """Swap a skill word for the posting's alias of the same skill, only inside this line."""
    out = text
    for hit in reversed(sheet.vocab.find(text)):
        wanted = surfaces.get(hit.id)
        # Phrases like "MCP server" would read badly if shortened, so only single words swap.
        single = " " not in hit.text and wanted is not None and " " not in wanted
        if wanted and single and wanted != hit.text:
            out = out[: hit.start] + wanted + out[hit.end :]
    return out


def _safe_phrase(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9 .&-]", "", value)[:60].strip()


def _pick(
    sheet: FactSheet,
    ids: list[str],
    wanted: set[str],
    surfaces: dict[str, str],
    limit: int,
    llm: LLM | None,
    keywords: list[str],
    rejected: list[str],
) -> list[ResumeBullet]:
    resolved = [b for i in ids if (b := sheet.resolve_bullet(i))]
    resolved.sort(key=lambda b: -_relevance(sheet, b.id, wanted))  # stable: ties keep order
    out = []
    for b in resolved[:limit]:
        text = mirror_aliases(sheet, b.text, surfaces)
        if llm is not None:
            text = _try_rewrite(sheet, llm, b.id, text, keywords, rejected)
        out.append(ResumeBullet(text, [b.id]))
    return out


def _try_rewrite(
    sheet: FactSheet, llm: LLM, fact_id: str, text: str, keywords: list[str], rejected: list[str]
) -> str:
    """A rewrite is kept only if the rule checks pass; otherwise the fact text is used."""
    user = data_block("line", text) + "\n" + data_block("keywords", ", ".join(keywords))
    try:
        proposed = ask(llm, Rewrite, system=REWRITE_SYSTEM, user=user).text.strip()
    except LLMError:
        return text
    problems = rule_violations(sheet, proposed, [fact_id])
    if problems:
        rejected.append(f"{fact_id}: {problems[0].code} {problems[0].detail}")
        return text
    return proposed


def _skill_lines(
    sheet: FactSheet, track: Track, wanted: set[str], surfaces: dict[str, str]
) -> list[SkillLineOut]:
    lines = []
    for line in sheet.data.base_resumes[track].skills:
        ids = [s for s in line.skills if sheet.skills[s].confirmed]
        ids.sort(key=lambda s: s not in wanted)
        if ids:
            names = [surfaces.get(s, sheet.skills[s].canonical) for s in ids]
            lines.append(SkillLineOut(line.label, ids, names))
    return lines


def _gaps(sheet: FactSheet, req: Requirements) -> list[Gap]:
    gaps = [Gap(t, "not on the fact sheet") for t in req.unknown]
    for sid in req.skills:
        if not sheet.skills[sid].confirmed:
            gaps.append(Gap(sheet.skills[sid].canonical, "on the fact sheet but unconfirmed"))
    return gaps


def check_resume(checker: TruthChecker, resume: TailoredResume) -> CheckReport:
    """Every line against its own facts, then the whole body once with the model pass."""
    found: list[Violation] = []
    units = [resume.summary] + [b for e in [*resume.roles, *resume.projects] for b in e.bullets]
    for unit in units:
        found += checker.check_unit(unit.text, unit.fact_ids, use_model=False).violations
    for line in resume.skill_lines:
        found += checker.check_unit(
            ", ".join(line.names), line.skill_ids, use_model=False
        ).violations
    if found:
        return CheckReport("block", found)
    body = body_markdown(resume, checker.sheet)
    return checker.check_unit(
        strip_contact(body, checker.sheet),
        resume.all_fact_ids(),
        extra_allowed=resume.labels(),
    )


def build_cover_note(
    sheet: FactSheet, resume: TailoredResume, title: str, company: str
) -> tuple[str, list[str], list[str]]:
    """A short note: fixed opening, then up to two verbatim fact lines. Returns text, ids, extra."""
    t, c = _safe_phrase(title), _safe_phrase(company)
    pool = [b for e in resume.roles + resume.projects for b in e.bullets]
    short = [b for b in pool if len(b.text) <= 220]
    first = (short or pool)[:2]
    body = " ".join(b.text.rstrip(".") + "." for b in first)
    note = f"I am applying for the {t} role at {c}. {body}".strip()
    return note, [i for b in first for i in b.fact_ids], [*t.split(), *c.split()]


def tailor(
    sheet: FactSheet,
    checker: TruthChecker,
    track: Track,
    *,
    title: str,
    company: str,
    posting_text: str,
    llm: LLM | None = None,
    role_max: int = 6,
    project_max: int = 4,
) -> TailorResult:
    clean, flagged = clean_posting_text(f"{title}\n{posting_text}")
    req = extract_requirements(sheet, clean)
    wanted = {s for s in req.skills if sheet.skills[s].confirmed}
    surfaces = {s: req.skills[s] for s in wanted}
    keywords = [sheet.skills[s].canonical for s in wanted]
    base = sheet.data.base_resumes[track]
    rejected: list[str] = []

    roles = []
    for exp in base.experience:
        bullets = _pick(sheet, exp.bullets, wanted, surfaces, role_max, llm, keywords, rejected)
        roles.append(ResumeEntry("role", exp.role, exp.title, bullets))
    projects = []
    for proj in base.projects:
        bullets = _pick(sheet, proj.bullets, wanted, surfaces, project_max, llm, keywords, rejected)
        if bullets:
            projects.append(ResumeEntry("project", proj.project, proj.name, bullets))
    projects.sort(
        key=lambda p: -sum(_relevance(sheet, i, wanted) for b in p.bullets for i in b.fact_ids)
    )

    resume = TailoredResume(
        track=track,
        summary=ResumeBullet(sheet.bullets[base.summary].text, [base.summary]),
        roles=roles,
        projects=projects,
        skill_lines=_skill_lines(sheet, track, wanted, surfaces),
        education_ids=list(base.education),
    )
    gaps = _gaps(sheet, req)
    warnings = []
    if not sheet.confirmed_answer("email"):
        warnings.append("contact details are unconfirmed, so the header is empty")
    report = check_resume(checker, resume)
    note, note_ids, extra = build_cover_note(sheet, resume, title, company)
    cover: str | None = note
    if checker.check_unit(note, note_ids, extra_allowed=extra).blocked:
        cover = None
        warnings.append("cover note blocked by the truthfulness check")
    return TailorResult(
        resume=resume,
        markdown=to_markdown(resume, sheet),
        gaps=gaps,
        gaps_md=gaps_markdown(title, company, gaps),
        cover_note=cover,
        report=report,
        injection_flagged=flagged,
        rejected_rewrites=rejected,
        warnings=warnings,
    )


def approve_markdown(
    checker: TruthChecker, ledger: ApprovalLedger, resume: TailoredResume, markdown: str
) -> CheckReport:
    """Final gate on the exact Markdown that will be submitted."""
    return _approve(checker, ledger, resume, markdown, markdown)


def approve_pdf(
    checker: TruthChecker, ledger: ApprovalLedger, resume: TailoredResume, pdf: bytes
) -> CheckReport:
    """Final gate on text re-extracted from the rendered PDF bytes."""
    return _approve(checker, ledger, resume, extract_pdf_text(pdf), pdf)


def approve_cover_note(
    checker: TruthChecker, ledger: ApprovalLedger, fact_ids: list[str], extra: list[str], note: str
) -> CheckReport:
    report = checker.check_unit(note, fact_ids, extra_allowed=extra)
    if not report.blocked:
        ledger.approve(note)
    return report


def approve_answer(
    checker: TruthChecker,
    ledger: ApprovalLedger,
    question: str,
    answer: str,
    cited: list[str],
) -> CheckReport:
    report = checker.check_answer(question, answer, cited)
    if not report.blocked:
        ledger.approve(answer)
    return report


def _approve(
    checker: TruthChecker,
    ledger: ApprovalLedger,
    resume: TailoredResume,
    text: str,
    artifact: str | bytes,
) -> CheckReport:
    body = strip_contact(text, checker.sheet)
    report = checker.check_unit(body, resume.all_fact_ids(), extra_allowed=resume.labels())
    if not report.blocked:
        ledger.approve(artifact)
    return report
