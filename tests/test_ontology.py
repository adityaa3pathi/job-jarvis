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


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("Large Language Models (LLMs)", "LLM"),
        ("RAG (hybrid retrieval)", "RAG"),
        ("Amazon Web Services", "AWS"),
        ("Embeddings & Vector Databases (pgvector)", "Embeddings & Vector Databases (pgvector)"),
    ],
)
def test_canonical_reads_either_half_of_a_bracketed_name(raw: str, name: str) -> None:
    assert canonical(raw) == name


def test_everyday_words_are_not_skills() -> None:
    assert mention("Node.js", "the planner node calls tools") is None
    assert mention("Node.js", "Node and React") == "Node"
    assert mention("REST", "idle the rest of the time") is None
    assert mention("REST", "RESTful APIs") == "RESTful"
    assert mention("JavaScript", "a Node.js service") is None
    assert mention("Go", "Hyderabad's go-to platform") is None
