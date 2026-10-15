"""One call for the whole tailoring run: job description, resume and evidence in, files out."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from jobagent.llm import LLM, LLMCache, model_id, short_cause
from jobagent.resume.evidence import EvidenceCard
from jobagent.resume.jd import JDProfile, SkillNeed, fit_check
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.latex import parse
from jobagent.resume.match import Coverage, match_profile
from jobagent.resume.ontology import mention
from jobagent.resume.rewrite import MAX_SKILLS, TailorResult, tailor_resume


@dataclass
class Outcome:
    profile: JDProfile
    coverage: Coverage
    tailored: TailorResult
    report: str
    keywords_before: float
    keywords_after: float
    jd_calls: int = 0
    jd_errors: list[str] = field(default_factory=list)


class _CountingLLM:
    """Counts model calls and keeps the cause of each failed one."""

    def __init__(self, inner: LLM) -> None:
        self.inner = inner
        self.model = model_id(inner)
        self.calls = 0
        self.errors: list[str] = []

    def complete_json(self, *, system: str, user: str) -> str:
        self.calls += 1
        try:
            return self.inner.complete_json(system=system, user=user)
        except Exception as exc:
            self.errors.append(short_cause(exc))
            raise


def keyword_coverage(text: str, needs: list[SkillNeed]) -> float:
    """Our own keyword score: share of the job's skill weight that appears in the resume text."""
    total = sum(n.weight for n in needs)
    found = sum(n.weight for n in needs if mention(n.name, text))
    return round(found / total, 2) if total else 0.0


def _prep_line(name: str, status: str) -> str:
    how = {
        "gap": "learn the basics and build one small project with it",
        "adjacent": "you know a related tool; learn how this one differs",
    }.get(status, "be ready to explain what it is and where you would use it")
    return f"- {name}: {how}. Expect an interview question on it."


def _causes(errors: list[str]) -> str:
    return "; ".join(f"{n}x {cause}" for cause, n in Counter(errors).most_common(3))


def build_report(out: Outcome, years_have: float | None) -> str:
    lines = [
        f"# Tailoring report: {out.profile.role_title or 'job'}",
        "",
        f"- Level: {out.profile.level}. Work mode: {out.profile.work_mode}.",
        f"- Evidence match: {out.coverage.score:.0%} of the job's weighted skills.",
        f"- Our keyword score (not a real ATS): {out.keywords_before:.0%} before, "
        f"{out.keywords_after:.0%} after.",
    ]
    if out.profile.degraded:
        lines += [
            "- WARNING: the model failed. Skills came from a plain scan and were not weighed."
        ]
    if out.profile.injection_flagged:
        lines += ["- WARNING: the posting contained text that tried to give instructions. Ignored."]
    if out.jd_errors:
        lines += [
            f"- WARNING: {len(out.jd_errors)} of {out.jd_calls} job-analysis model calls failed "
            f"({_causes(out.jd_errors)}). The skill list may be less complete."
        ]
    notes = fit_check(out.profile, years_have) if years_have is not None else []
    lines += [f"- Fit: {n}" for n in notes]
    added = out.tailored.added_gap_skills
    lines += ["", "## Added to your Skills but NOT backed by your work", ""]
    if added:
        lines += [f"- {s}" for s in added]
        lines += ["", "If an interviewer asks about these, you must be ready. Prepare first."]
    else:
        lines += ["- None."]
    if out.tailored.dropped_skills:
        lines += ["", "## Removed from your Skills", ""]
        lines += [f"- {s}" for s in out.tailored.dropped_skills]
        lines += [
            "",
            f"Skills keeps at most {MAX_SKILLS}, best for this job first, and only skills your "
            "evidence backs. Add any back by hand if you need them.",
        ]
    status = {i.need.name: i.status for i in out.coverage.items}
    prep = out.coverage.prep
    lines += ["", "## Prepare before the interview", ""]
    lines += [_prep_line(n, status[n]) for n in prep] or ["- Nothing missing."]
    lines += ["", "## Bullet changes", ""]
    bullets = [c for c in out.tailored.changes if c.kind == "bullet"]
    for c in bullets:
        lines += [f"- Before: {c.before}", f"  After: {c.after}"]
    if not bullets:
        lines += ["- None."]
    if any(c.kind == "reorder" for c in out.tailored.changes):
        lines += ["", "Bullets were reordered so the most relevant come first."]
    failed = out.tailored.failed
    if failed:
        why = _causes([cause for _, cause in failed])
        lines += ["", f"- {len(failed)} bullets kept unchanged: model error ({why})"]
    if out.tailored.rejected:
        lines += ["", "## Rewrites refused by the fact check", ""]
        lines += [f"- {reason}: {before}" for before, reason in out.tailored.rejected]
    return "\n".join(lines) + "\n"


def run_tailor(
    jd_text: str,
    latex_text: str,
    cards: list[EvidenceCard],
    llm: LLM,
    *,
    writer_llm: LLM | None = None,
    add_gaps: bool = True,
    years_have: float | None = None,
    profile: JDProfile | None = None,
    cache: LLMCache | None = None,
) -> Outcome:
    """Pass `profile` when the JD was already analysed, to skip a second model round."""
    resume = parse(latex_text)
    counted = _CountingLLM(llm)
    profile = profile or analyze_jd(jd_text, counted, cache)
    coverage = match_profile(profile, cards)
    tailored = tailor_resume(resume, cards, profile, coverage, writer_llm or llm, add_gaps)
    explicit = profile.skills
    out = Outcome(
        profile=profile,
        coverage=coverage,
        tailored=tailored,
        report="",
        keywords_before=keyword_coverage(latex_text, explicit),
        keywords_after=keyword_coverage(tailored.latex, explicit),
        jd_calls=counted.calls,
        jd_errors=counted.errors,
    )
    out.report = build_report(out, years_have)
    return out
