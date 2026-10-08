from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from jobagent.config import REPO_ROOT
from jobagent.facts import FactsError, FactsFile, FactSheet, check_sheet, load_facts

RESUME_DIR = REPO_ROOT.parent
MARKED_BULLETS = [
    "ai-docnow-1", "ai-docnow-2", "ai-docnow-3", "ai-docnow-4", "ai-docnow-6", "ai-badaya-1",
    "fs-docnow-1", "fs-docnow-5", "fs-docnow-6", "fs-docnow-7", "fs-docnow-8", "fs-docnow-9",
    "fs-pigeon-1",
]  # fmt: skip
MARKED_NUMBERS = [
    "147", "99.99%", "99.9%", "40%", "97%", "1100+", "80", "55%", "82%", "9%", "2%", "50",
    "26", "30", "16", "19",
]  # fmt: skip


@pytest.fixture(scope="module")
def sheet() -> FactSheet:
    return load_facts(REPO_ROOT / "facts.yaml")


def raw() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load((REPO_ROOT / "facts.yaml").read_text())
    return loaded


def test_real_fact_sheet_loads(sheet: FactSheet) -> None:
    assert sheet.hash
    assert set(sheet.data.base_resumes) == {"ai", "full_stack"}


@pytest.mark.parametrize("bullet_id", MARKED_BULLETS)
def test_marked_claims_start_unconfirmed(sheet: FactSheet, bullet_id: str) -> None:
    assert not sheet.bullets[bullet_id].confirmed


def test_marked_numbers_cannot_be_used(sheet: FactSheet) -> None:
    usable = sheet.usable_numbers()
    for token in MARKED_NUMBERS:
        assert token not in usable, token
    assert {"2500+", "50+", "300+"} <= set(usable)


def test_all_standard_answers_start_unconfirmed(sheet: FactSheet) -> None:
    assert not any(a.confirmed for a in sheet.answers.values())
    assert sheet.confirmed_answer("email") is None


def test_unconfirmed_only_skills(sheet: FactSheet) -> None:
    for sid in ("axios", "csrf", "aws-s3", "healthians", "whatsapp", "pusher"):
        assert not sheet.skills[sid].confirmed
    assert sheet.skills["razorpay"].confirmed


def _strip_markers(text: str) -> str:
    return re.sub(r"\s*\*\([^)]*\)\*", "", text)


def test_every_bullet_is_verbatim_from_a_resume(sheet: FactSheet) -> None:
    files = [RESUME_DIR / "resume-ai-engineer.md", RESUME_DIR / "resume-full-stack.md"]
    if not all(f.exists() for f in files):
        pytest.skip("resume files not next to the repo")
    corpus = "\n".join(_strip_markers(f.read_text()) for f in files)
    corpus = re.sub(r"^- ", "", corpus, flags=re.M)
    for b in sheet.bullets.values():
        text = b.text.replace('\\"', '"')
        if b.kind == "summary":
            continue
        assert text in corpus, f"{b.id} is not in the resumes"
    for b in sheet.bullets.values():
        if b.kind == "summary":
            assert b.text in corpus, b.id


def test_fallback_used_only_when_confirmed() -> None:
    data = raw()
    _find(data, "bullets", "ai-badaya-1")["fallback"] = "fs-badaya-1"
    sheet = check_sheet(FactsFile.model_validate(data))
    resolved = sheet.resolve_bullet("ai-badaya-1")
    assert resolved is not None
    assert resolved.id == "fs-badaya-1"
    assert sheet.resolve_bullet("ai-docnow-1") is None
    assert sheet.resolve_bullet("ai-docnow-5") is not None


def test_experience_years_rounds_down(sheet: FactSheet) -> None:
    assert sheet.experience_years(date(2026, 10, 8)) == 2
    assert sheet.experience_years(date(2026, 5, 1)) == 1
    assert sheet.experience_years(date(2026, 5, 1), "up") == 2


def test_llm_view_has_no_contact_details(sheet: FactSheet) -> None:
    blob = str(sheet.llm_view(list(sheet.bullets) + list(sheet.skills)))
    for private in ("9000000001", "example.com"):
        assert private not in blob


def _mutated(mutate) -> None:  # type: ignore[no-untyped-def]
    data = raw()
    mutate(data)
    check_sheet(FactsFile.model_validate(data))


def _find(data: dict[str, object], table: str, fid: str) -> dict[str, object]:
    return next(x for x in data[table] if x["id"] == fid)  # type: ignore[attr-defined,union-attr]


def test_skill_without_evidence_rejected() -> None:
    def mutate(d: dict[str, object]) -> None:
        _find(d, "skills", "python")["evidence"] = []

    with pytest.raises(FactsError, match="no evidence"):
        _mutated(mutate)


def test_confirmed_skill_needs_confirmed_evidence() -> None:
    def mutate(d: dict[str, object]) -> None:
        _find(d, "skills", "axios")["confirmed"] = True

    with pytest.raises(FactsError, match="without confirmed evidence"):
        _mutated(mutate)


@pytest.mark.parametrize("key", ["full_name", "email"])
def test_missing_required_answers_fail_loudly(key: str) -> None:
    def mutate(d: dict[str, object]) -> None:
        d["answers"] = [a for a in d["answers"] if a["key"] != key]  # type: ignore[attr-defined,union-attr]

    with pytest.raises(FactsError, match=f"missing required answer: {key}"):
        _mutated(mutate)


def test_confirmed_bullet_with_unconfirmed_number_rejected() -> None:
    def mutate(d: dict[str, object]) -> None:
        _find(d, "bullets", "ai-docnow-6")["confirmed"] = True

    with pytest.raises(FactsError, match="unconfirmed"):
        _mutated(mutate)


def test_number_in_text_needs_a_number_fact() -> None:
    def mutate(d: dict[str, object]) -> None:
        _find(d, "bullets", "ai-docnow-5")["numbers"] = []

    with pytest.raises(FactsError, match="has no number fact"):
        _mutated(mutate)


def test_undeclared_scope_word_rejected() -> None:
    def mutate(d: dict[str, object]) -> None:
        _find(d, "bullets", "fs-badaya-1")["scope_words"] = []

    with pytest.raises(FactsError, match="scope words"):
        _mutated(mutate)


def test_duplicate_alias_and_id_rejected() -> None:
    def alias(d: dict[str, object]) -> None:
        _find(d, "skills", "react")["aliases"] = ["React", "Java"]

    with pytest.raises(FactsError, match="alias"):
        _mutated(alias)

    def dup(d: dict[str, object]) -> None:
        _find(d, "skills", "react")["id"] = "python"

    with pytest.raises(FactsError, match="duplicate id"):
        _mutated(dup)


def test_answer_value_must_be_allowed(tmp_path: Path) -> None:
    def mutate(d: dict[str, object]) -> None:
        _find(d, "answers", "ans-grad-year")["value"] = "2030"

    with pytest.raises(FactsError, match="allowed_values"):
        _mutated(mutate)
