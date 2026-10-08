from __future__ import annotations

from jobagent.resume.evidence import (
    cards_from_manifests,
    cards_from_summary,
    merge_cards,
)


def skills(cards) -> set[str]:  # type: ignore[no-untyped-def]
    return {c.text for c in cards if c.kind == "skill"}


def test_requirements_txt_maps_to_skill_names() -> None:
    text = "fastapi>=0.115\npgvector==0.3\nlangfuse[extra]>=2\n# comment\nunknown-lib\n"
    cards = cards_from_manifests({"ai/requirements.txt": text}, "github:o/r")
    assert skills(cards) == {"FastAPI", "pgvector", "Langfuse"}
    assert all(c.sources == ("github:o/r:ai/requirements.txt",) for c in cards)


def test_package_json_reads_dependencies_and_dev_dependencies() -> None:
    text = (
        '{"dependencies": {"next": "15", "@trpc/server": "11"}, "devDependencies": {"prisma": "6"}}'
    )
    cards = cards_from_manifests({"package.json": text}, "github:o/r")
    assert skills(cards) == {"Next.js", "tRPC", "Prisma"}


def test_pyproject_reads_dependencies_groups_and_extras() -> None:
    text = (
        '[project]\ndependencies = ["peft>=0.12", "vllm>=0.6"]\n'
        '[project.optional-dependencies]\nx = ["langchain>=0.3"]\n'
        '[dependency-groups]\ndev = ["pytest>=8"]\n'
    )
    cards = cards_from_manifests({"pyproject.toml": text}, "github:o/r")
    assert skills(cards) == {"PEFT", "vLLM", "LangChain", "pytest"}


def test_broken_manifest_is_skipped_not_fatal() -> None:
    cards = cards_from_manifests(
        {"package.json": "{not json", "pyproject.toml": "[["}, "github:o/r"
    )
    assert cards == []


def test_summary_becomes_owner_claims_and_skills() -> None:
    text = "Built RAG pipelines with FastAPI and pgvector.\nServed 2500+ users."
    cards = cards_from_summary(text)
    claims = [c for c in cards if c.kind == "claim"]
    assert [c.text for c in claims] == [
        "Built RAG pipelines with FastAPI and pgvector.",
        "Served 2500+ users.",
    ]
    assert all(c.confirmed and c.sources == ("owner",) for c in cards)
    assert {"FastAPI", "pgvector"} <= skills(cards)


def test_summary_skill_match_respects_word_boundaries() -> None:
    assert skills(cards_from_summary("I like reactive programming and nextdoor.")) == set()


def test_merge_joins_sources_and_keeps_confirmation() -> None:
    a = cards_from_manifests({"requirements.txt": "fastapi"}, "github:o/a")
    b = cards_from_manifests({"requirements.txt": "fastapi"}, "github:o/b")
    c = cards_from_summary("I use FastAPI daily.")
    merged = merge_cards(a + b + c)
    fast = [m for m in merged if m.text == "FastAPI"]
    assert len(fast) == 1
    assert set(fast[0].sources) == {
        "github:o/a:requirements.txt",
        "github:o/b:requirements.txt",
        "owner",
    }
    assert fast[0].confirmed


def test_card_ids_are_stable() -> None:
    one = cards_from_summary("I use FastAPI daily.")
    two = cards_from_summary("I use FastAPI daily.")
    assert [c.id for c in one] == [c.id for c in two]
