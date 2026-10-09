from __future__ import annotations

from jobagent.resume.evidence import make_card
from jobagent.resume.jd import JDProfile, SkillNeed
from jobagent.resume.match import match_profile


def need(name: str, importance: str = "must", weight: float = 1.0, implicit: bool = False):  # type: ignore[no-untyped-def]
    return SkillNeed(name, name, importance, weight, implicit)


CARDS = [
    make_card("skill", "Python", "github:o/r:languages", False),
    make_card("skill", "LangChain", "github:o/r:requirements.txt", False),
    make_card("claim", "Built RAG pipelines for 2500 users.", "owner", True),
    make_card("project", "demo: Kubernetes operator toy", "github:o/demo", False),
]


def by_name(result):  # type: ignore[no-untyped-def]
    return {item.need.name: item for item in result.items}


def test_exact_skill_is_proven_with_its_evidence() -> None:
    result = match_profile(JDProfile(skills=[need("Python")]), CARDS)
    item = by_name(result)["Python"]
    assert item.status == "proven"
    assert item.card_ids == [CARDS[0].id]


def test_same_family_is_adjacent_and_names_the_related_skill() -> None:
    item = by_name(match_profile(JDProfile(skills=[need("LangGraph")]), CARDS))["LangGraph"]
    assert item.status == "adjacent"
    assert item.related == ["LangChain"]


def test_owner_claim_or_project_text_that_names_the_skill_is_proven() -> None:
    result = by_name(match_profile(JDProfile(skills=[need("RAG"), need("Kubernetes")]), CARDS))
    assert result["RAG"].status == "proven"
    assert result["Kubernetes"].status == "proven"


def test_nothing_related_is_a_gap() -> None:
    assert (
        by_name(match_profile(JDProfile(skills=[need("Terraform")]), CARDS))["Terraform"].status
        == "gap"
    )


def test_auto_add_list_has_only_explicit_must_have_gaps() -> None:
    profile = JDProfile(
        skills=[need("Terraform"), need("Kafka", "nice"), need("Python")],
        implicit_skills=[need("Tool calling", "nice", 0.3, True)],
    )
    result = match_profile(profile, CARDS)
    assert result.auto_add == ["Terraform"]
    assert [i.need.name for i in result.items if i.need.implicit] == ["Tool calling"]


def test_score_counts_adjacent_as_half_and_ignores_implicit() -> None:
    profile = JDProfile(
        skills=[need("Python", weight=1.0), need("LangGraph", weight=1.0), need("Terraform")],
        implicit_skills=[need("Tool calling", "nice", 0.3, True)],
    )
    assert match_profile(profile, CARDS).score == round((1.0 + 0.5) / 3.0, 2)


def test_empty_profile_scores_zero() -> None:
    assert match_profile(JDProfile(), CARDS).score == 0.0


def test_prep_list_covers_adjacent_gaps_and_implicit_skills() -> None:
    profile = JDProfile(
        skills=[need("LangGraph"), need("Terraform"), need("Python")],
        implicit_skills=[need("Tool calling", "nice", 0.3, True)],
    )
    prep = match_profile(profile, CARDS).prep
    assert prep == ["LangGraph", "Terraform", "Tool calling"]
