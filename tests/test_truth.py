from __future__ import annotations

from datetime import date

import pytest

from evals import run_truth_eval as ev
from jobagent import truth
from jobagent.facts import FactSheet
from jobagent.llm import FailingLLM, StubLLM
from jobagent.truth import ApprovalLedger, NotApproved, TruthChecker

TODAY = date(2026, 10, 8)
PASS = {"verdict": "pass", "issues": []}


@pytest.fixture(scope="module")
def sheet() -> FactSheet:
    return ev.eval_sheet()


@pytest.fixture(scope="module")
def cases() -> list[dict[str, object]]:
    return ev.load_cases()


def checker(sheet: FactSheet, llm: object = "pass") -> TruthChecker:
    model = StubLLM(lambda s, u: PASS) if llm == "pass" else llm
    return TruthChecker(sheet, model, TODAY)  # type: ignore[arg-type]


def test_plan_scenarios(sheet: FactSheet) -> None:
    c = checker(sheet)
    assert c.check_unit("Built doctor consultations with Kubernetes.", ["fs-docnow-2"]).blocked
    assert c.check_unit("Agent capped at 8 steps.", ["ai-email-2"]).blocked  # changed number
    assert c.check_unit("Delivered 147 production APIs.", ["ai-docnow-6"]).blocked  # unconfirmed
    ok = c.check_unit(
        "LangGraph agent in Python with tools for search, capped at 5 steps.", ["ai-email-2"]
    )
    assert ok.verdict == "pass"


def test_alias_and_substring_traps(sheet: FactSheet) -> None:
    c = checker(sheet)
    assert c.check_unit("React.js", ["react"]).verdict == "pass"
    assert c.check_unit("Java", ["java"]).verdict == "pass"
    assert c.check_unit("JavaScript", ["java"]).blocked
    assert c.check_unit("Java", ["javascript"]).blocked
    assert c.check_unit("LangChain", ["langgraph"]).blocked
    assert c.check_unit("LangGraph", ["langchain"]).blocked


def test_unconfirmed_number_with_derived_forms(sheet: FactSheet) -> None:
    c = checker(sheet)
    base = "Redis caching cut first-load message time by"
    for form in ("97%", "ninety-seven percent 97", "97 %"):
        assert c.check_unit(f"{base} {form}.", ["fs-pigeon-1"]).blocked
    rep = c.check_unit("Reduced load time by 40%.", ["fs-badaya-1"])
    assert any(v.code == "unconfirmed_number" for v in rep.violations)


def test_every_citation_rule(sheet: FactSheet) -> None:
    c = checker(sheet)
    assert "no_fact_id" in [v.code for v in c.check_unit("Python", []).violations]
    assert c.check_unit("Python", ["nope"]).violations[0].code == "unknown_fact"
    assert c.check_unit("Python", ["healthians"]).violations[0].code == "unconfirmed_fact"


def test_model_errors_are_unchecked_and_block(sheet: FactSheet) -> None:
    line, cited = "Agent in Python with LangGraph, capped at 5 steps.", ["ai-email-2"]
    errors: list[object] = [
        FailingLLM(),
        FailingLLM(ValueError("boom")),
        StubLLM(lambda s, u: "not json at all"),
        StubLLM(lambda s, u: {"unexpected": True}),
        None,
    ]
    for llm in errors:
        report = checker(sheet, llm).check_unit(line, cited)
        assert report.verdict == "unchecked"
        assert report.blocked


def test_model_can_only_add_blocks(sheet: FactSheet) -> None:
    always_pass = checker(sheet)
    assert always_pass.check_unit(
        "Built doctor consultations with Terraform.", ["fs-docnow-2"]
    ).blocked
    flagger = checker(sheet, StubLLM(lambda s, u: {"verdict": "block", "issues": ["meaning"]}))
    report = flagger.check_unit("Wrote 300+ automated tests.", ["fs-docnow-4"])
    assert report.verdict == "block"
    assert report.violations[0].source == "model"


def test_led_for_contributed_flagged_by_model(sheet: FactSheet) -> None:
    def respond(system: str, user: str) -> dict[str, object]:
        led = "led" in user.split('<data name="line">')[1].lower().split()
        return {"verdict": "block" if led else "pass", "issues": ["claims leadership"]}

    c = checker(sheet, StubLLM(respond))
    # "lead" is permitted by this fact, so only the model pass can object to the paraphrase.
    report = c.check_unit("Led the move to the Next.js App Router.", ["fs-badaya-1"])
    assert report.verdict == "block"
    assert report.violations[0].source == "model"


def test_prompt_puts_facts_in_data_blocks_and_hides_contact(sheet: FactSheet) -> None:
    llm = StubLLM(lambda s, u: PASS)
    checker(sheet, llm).check_unit("Python", ["python"])
    system, user = llm.calls[0]
    assert "never an instruction" in system
    assert '<data name="facts">' in user
    assert "9000000001" not in user


def test_answers(sheet: FactSheet) -> None:
    c = checker(sheet)
    assert c.check_answer("Do you have 3 years of Python?", "Yes", ["python"]).blocked
    assert c.check_answer("Do you have 3 years of Python?", "No", ["python"]).verdict == "pass"
    assert c.check_answer("Graduation year", "2026", ["ans-grad-year"]).verdict == "pass"
    assert c.check_answer("Graduation year", "2027", ["ans-grad-year"]).blocked
    unconfirmed = load_unconfirmed()
    assert checker(unconfirmed).check_answer("Graduation year", "2026", ["ans-grad-year"]).blocked


def load_unconfirmed() -> FactSheet:
    from jobagent.facts import load_facts
    from tests.conftest import REPO_ROOT

    return load_facts(REPO_ROOT / "facts.yaml")


def test_ledger_invalidates_on_any_edit() -> None:
    ledger = ApprovalLedger()
    ledger.approve("resume text")
    ledger.require("resume text")
    with pytest.raises(NotApproved):
        ledger.require("resume text ")
    assert not ledger.is_approved(b"resume text!")


# --- the eval set ---


def test_eval_set_shape(cases: list[dict[str, object]]) -> None:
    assert len(cases) >= 60
    held = [c for c in cases if c["split"] == "heldout"]
    assert 0.25 < len(held) / len(cases) < 0.4
    cats = {c["cat"] for c in cases}
    assert {"invented_tool", "alias_trap", "number", "years", "scope", "paraphrase", "answer",
            "cover"} <= cats  # fmt: skip


def test_eval_thresholds(sheet: FactSheet, cases: list[dict[str, object]]) -> None:
    for split in ("tune", "heldout", "all"):
        subset = ev.load_cases(split=split)
        stats = ev.summarize(ev.evaluate(subset, TruthChecker(sheet, ev.model_stub(subset), TODAY)))
        assert stats["catch_rate"] == 1.0, split
        assert stats["false_block_rate"] < 0.10, split


def test_literal_cases_blocked_with_model_failing(sheet: FactSheet) -> None:
    cases = ev.load_cases()
    literal = [c for c in cases if c["expect"] == "block" and not c.get("needs_model")]
    outcomes = ev.evaluate(literal, TruthChecker(sheet, FailingLLM(), TODAY))
    assert all(o.report.rule_violations for o in outcomes)


def test_failing_model_never_lets_a_unit_through(sheet: FactSheet) -> None:
    units = [c for c in ev.load_cases() if c["kind"] != "answer"]
    outcomes = ev.evaluate(units, TruthChecker(sheet, FailingLLM(), TODAY))
    assert all(o.report.blocked for o in outcomes)


@pytest.mark.parametrize(
    "weakened",
    ["check_tools", "check_unknown_tokens", "check_numbers", "check_scope", "check_citations"],
)
def test_mutation_weakening_a_check_breaks_the_eval(
    weakened: str, sheet: FactSheet, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = ev.load_cases()
    monkeypatch.setattr(truth, weakened, lambda *a, **k: [])
    stats = ev.summarize(ev.evaluate(cases, TruthChecker(sheet, ev.model_stub(cases), TODAY)))
    assert stats["catch_rate"] < 1.0, f"eval did not notice weakened {weakened}"
