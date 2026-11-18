from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from jobagent.llm import FailingLLM, StubLLM
from jobagent.resume.evidence import make_card
from jobagent.resume.jd import JDProfile, SkillNeed
from jobagent.resume.latex import from_plain, parse, to_plain
from jobagent.resume.match import match_profile
from jobagent.resume.rewrite import build_skills, check_rewrite, tailor_resume

SAMPLE = (Path(__file__).parent / "fixtures" / "resume" / "sample.tex").read_text()


def need(name: str, importance: str = "must", weight: float = 1.0) -> SkillNeed:
    return SkillNeed(name, name, importance, weight)


CARDS = [
    make_card("skill", s, "github:o/r", False)
    for s in ("Python", "FastAPI", "LangChain", "Langfuse", "RAGAS", "PostgreSQL", "Next.js")
]
PROFILE = JDProfile(
    role_title="AI Engineer",
    skills=[
        need("Python", weight=1.3),
        need("LangGraph"),
        need("Terraform"),
        need("Langfuse", "nice"),
    ],
)


def bullets_in(user: str) -> list[tuple[int, str]]:
    """The numbered bullets sent in one batched rewrite prompt."""
    block = user.split('<data name="bullets">\n', 1)[1].split("\n</data>", 1)[0]
    return [(int(m[1]), m[2]) for m in re.finditer(r"^(\d+)\. (.*)$", block, re.M)]


def same_text_for_every_bullet(text: str) -> StubLLM:
    return StubLLM(lambda s, u: {"items": [{"id": i, "text": text} for i, _ in bullets_in(u)]})


def run(reply: Any = None, add_gaps: bool = True, llm: Any = None):  # type: ignore[no-untyped-def]
    llm = llm or same_text_for_every_bullet((reply or {"text": "unused"})["text"])
    resume = parse(SAMPLE)
    return tailor_resume(resume, CARDS, PROFILE, match_profile(PROFILE, CARDS), llm, add_gaps)


def test_plain_text_round_trip_keeps_bold_arrows_and_escapes() -> None:
    arg = r"Cut cost \textbf{9\% $\rightarrow$ 2\%} for R\&D"
    plain = to_plain(arg)
    assert plain == "Cut cost **9% → 2%** for R&D"
    assert from_plain(plain) == arg


def test_unknown_latex_commands_are_not_rewritten() -> None:
    assert to_plain(r"see \href{x}{y}") is None


def test_check_accepts_same_facts_with_new_wording() -> None:
    assert (
        check_rewrite(
            "Built APIs with FastAPI cutting latency by 40%",
            "Engineered FastAPI APIs, cutting latency by 40%",
            set(),
        )
        is None
    )


@pytest.mark.parametrize(
    ("new", "reason"),
    [
        ("Built APIs with FastAPI cutting latency by 60%", "new number"),
        ("Led APIs with FastAPI cutting latency by 40%", "bigger role"),
        ("Built APIs with FastAPI and Kafka cutting latency by 40%", "new tool"),
        ("", "empty"),
        ("Built APIs with FastAPI cutting latency by 40% " + "x" * 400, "too long"),
    ],
)
def test_check_refuses_new_facts(new: str, reason: str) -> None:
    got = check_rewrite("Built APIs with FastAPI cutting latency by 40%", new, set())
    assert got is not None and reason in got


def test_check_allows_a_tool_the_evidence_proves() -> None:
    assert (
        check_rewrite("Built the search API", "Built the search API with FastAPI", {"FastAPI"})
        is None
    )


def test_good_rewrite_is_applied_and_everything_else_is_untouched() -> None:
    plain = to_plain(parse(SAMPLE).section("Experience").commands("resumeItem")[-1].args[0])
    assert plain is not None
    result = run({"text": "Rebuilt " + plain.removeprefix("Migrated the app to the ")})
    assert result.latex != SAMPLE
    out = parse(result.latex)
    assert out.preamble == parse(SAMPLE).preamble
    assert out.section("Education").emit() == parse(SAMPLE).section("Education").emit()
    assert any(c.kind == "bullet" for c in result.changes)


def test_rewrite_with_a_new_number_is_rejected_and_original_kept() -> None:
    result = run({"text": "Cut response time by 99%"})
    assert result.rejected
    assert "Cut response time by 99" not in result.latex


def test_model_failure_keeps_every_bullet() -> None:
    result = run(llm=FailingLLM())
    before = parse(SAMPLE)
    after = parse(result.latex)
    for title in ("Experience", "Projects"):
        old = sorted(c.args[0] for c in before.section(title).commands("resumeItem"))
        new = sorted(c.args[0] for c in after.section(title).commands("resumeItem"))
        assert new == old
    assert not [c for c in result.changes if c.kind == "bullet"]


def test_skills_put_job_terms_first_and_add_must_have_gaps_last() -> None:
    skills, gaps = build_skills(CARDS, match_profile(PROFILE, CARDS))
    assert skills[0] == "Python"
    assert "Terraform" in skills and gaps == ["Terraform"]
    assert "LangGraph" not in skills  # adjacent only, not a claim
    assert skills.index("Terraform") > skills.index("Langfuse")


def test_no_gaps_switch() -> None:
    skills, gaps = build_skills(CARDS, match_profile(PROFILE, CARDS), add_gaps=False)
    assert "Terraform" not in skills and gaps == []


def test_skills_are_capped_and_gaps_survive_the_cap() -> None:
    many = [make_card("skill", f"Tool{i}", "owner", True) for i in range(30)]
    profile = JDProfile(skills=[need("Terraform")])
    skills, gaps = build_skills(many, match_profile(profile, many), cap=15)
    assert len(skills) == 15 and "Terraform" in skills and gaps == ["Terraform"]


def test_tailored_resume_has_a_rebuilt_skills_section_and_a_gap_report() -> None:
    result = run()
    skills_tex = parse(result.latex).section("Technical Skills").emit()
    assert "Terraform" in skills_tex and "Python" in skills_tex
    assert result.added_gap_skills == ["Terraform"]
    assert result.latex.count("\\begin{document}") == 1
    assert parse(result.latex).emit() == result.latex


def test_model_failure_is_recorded_with_its_cause() -> None:
    result = run(llm=FailingLLM())
    assert result.failed
    assert all("TimeoutError" in cause for _, cause in result.failed)


def test_owner_skills_stay_after_the_job_skills() -> None:
    result = run()
    kept = parse(result.latex).section("Technical Skills").emit()
    assert "Kubernetes" in kept and "TypeScript" in kept  # the owner's own list is kept
    assert kept.index("Python") < kept.index("Kubernetes")
    assert result.dropped_skills == []


def test_skills_cut_by_the_cap_are_listed() -> None:
    many = [make_card("skill", f"Tool{i}", "owner", True) for i in range(60)]
    coverage = match_profile(PROFILE, many)
    result = tailor_resume(parse(SAMPLE), many, PROFILE, coverage, FailingLLM())
    kept = parse(result.latex).section("Technical Skills").emit()
    cut = [f"Tool{i}" for i in range(60) if not re.search(rf"\bTool{i}\b", kept)]
    assert len(cut) >= 20
    assert set(cut) <= set(result.dropped_skills)


def test_proven_job_skills_are_never_cut_for_gaps_or_owner_extras() -> None:
    owner = [make_card("skill", f"Tool{i}", "owner", True) for i in range(40)]
    cards = [*CARDS, *owner]
    profile = JDProfile(skills=[need(s) for s in ("Python", "FastAPI", "PostgreSQL", "Terraform")])
    skills, _ = build_skills(cards, match_profile(profile, cards), cap=5, add_gaps=True)
    assert skills[:3] == ["Python", "FastAPI", "PostgreSQL"] and "Terraform" in skills


def test_skills_are_grouped_by_kind() -> None:
    from jobagent.resume.rewrite import _bucket

    assert _bucket("SQL") == "Languages" and _bucket("Python") == "Languages"
    assert _bucket("PyTorch") == "AI \\& ML" and _bucket("Vector Databases") == "AI \\& ML"
    assert _bucket("FastAPI") == "Backend \\& Web" and _bucket("REST APIs") == "Backend \\& Web"
    assert (
        _bucket("Docker") == "Data, Cloud \\& DevOps"
        and _bucket("NoSQL") == "Data, Cloud \\& DevOps"
    )
    assert _bucket("Agile") == "Tools \\& Practices"


def summary_llm(summary: str) -> StubLLM:
    def respond(system: str, user: str) -> Any:
        if "Rewrite the summary" in system:
            return {"text": summary}
        return {"items": []}

    return StubLLM(respond)


def test_summary_is_tailored_when_it_adds_no_facts() -> None:
    new = (
        "AI engineer with **2500+ healthcare users** in production: LangGraph agents, hybrid "
        "RAG, eval harnesses gating CI and Langfuse observability. Fine-tuned Mistral-7B with "
        "QLoRA, served via vLLM on Kubernetes; answer quality **55% → 82%**, wrong responses "
        "**9% → 2%**."
    )
    result = tailor_resume(
        parse(SAMPLE), CARDS, PROFILE, match_profile(PROFILE, CARDS), summary_llm(new)
    )
    assert result.summary is not None and result.summary.after == new
    assert "\\textbf{2500+ healthcare users} in production" in result.latex
    assert parse(result.latex).emit() == result.latex


@pytest.mark.parametrize(
    ("new", "reason"),
    [
        (
            "AI engineer with 5 years leading teams of 10 building agents for 2500+ users.",
            "new number",
        ),
        ("AI engineer who built agents on Terraform for 2500+ healthcare users.", "new tool"),
    ],
)
def test_summary_that_adds_facts_is_refused(new: str, reason: str) -> None:
    result = tailor_resume(
        parse(SAMPLE), CARDS, PROFILE, match_profile(PROFILE, CARDS), summary_llm(new)
    )
    assert result.summary is None and reason in result.summary_note
    assert "AI engineer who took a multi-agent agentic system" in result.latex


def test_a_bullet_already_past_the_cap_may_be_tightened_but_not_grown() -> None:
    long = "Built APIs with FastAPI for 2500+ users, " + "with retries and queues " * 14
    assert len(long) > 320
    assert check_rewrite(long, long[:-30], set()) is None
    assert check_rewrite(long, long + " and more", set()) == "too long"
