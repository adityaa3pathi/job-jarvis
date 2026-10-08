from __future__ import annotations

import pytest

from jobagent.resume.ontology import canonical, mention, related


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("k8s", "Kubernetes"),
        ("Postgres", "PostgreSQL"),
        ("nodejs", "Node.js"),
        ("NextJS", "Next.js"),
        ("large language models", "LLM"),
        ("fastapi", "FastAPI"),
        ("Brand New Tool", "Brand New Tool"),
    ],
)
def test_canonical(raw: str, name: str) -> None:
    assert canonical(raw) == name


def test_mention_returns_the_spelling_used_in_the_text() -> None:
    assert mention("Kubernetes", "we run k8s in prod") == "k8s"
    assert mention("Kubernetes", "we run Kubernetes") == "Kubernetes"
    assert mention("Kubernetes", "we run docker") is None


def test_mention_needs_whole_words() -> None:
    assert mention("Go", "good ongoing work") is None
    assert mention("Go", "experience in Go or Rust") == "Go"
    assert mention("React", "reactive programming") is None


def test_related_skills_share_a_family() -> None:
    assert related("LangGraph", "LangChain")
    assert related("pgvector", "FAISS")
    assert not related("LangGraph", "Kubernetes")
    assert not related("Python", "Python")  # same skill is a match, not "related"
