"""Truthfulness eval. Usage: uv run python evals/run_truth_eval.py [--split tune|heldout|all]."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobagent.facts import FactSheet, load_facts  # noqa: E402
from jobagent.llm import FailingLLM, StubLLM  # noqa: E402
from jobagent.truth import CheckReport, TruthChecker  # noqa: E402

TODAY = date(2026, 10, 8)


@dataclass
class Outcome:
    case: dict[str, Any]
    report: CheckReport

    @property
    def correct(self) -> bool:
        return (self.report.verdict == "pass") == (self.case["expect"] == "pass")


def load_cases(path: Path | None = None, split: str = "all") -> list[dict[str, Any]]:
    data = yaml.safe_load((path or ROOT / "evals" / "truth_cases.yaml").read_text())
    return [c for c in data["cases"] if split == "all" or c["split"] == split]


def eval_sheet() -> FactSheet:
    """Real fact sheet with the standard answers marked confirmed, as if the owner had."""
    sheet = load_facts(_facts_path())
    for answer in sheet.answers.values():
        answer.confirmed = True
    return sheet


def model_stub(cases: list[dict[str, Any]]) -> StubLLM:
    """Stands in for a real model: blocks only the lines marked needs_model, passes the rest."""
    hard = {c["text"] for c in cases if c.get("needs_model")}

    def respond(system: str, user: str) -> dict[str, Any]:
        for text in hard:
            if text in user:
                return {"verdict": "block", "issues": ["meaning changed"]}
        return {"verdict": "pass", "issues": []}

    return StubLLM(respond)


def run_case(checker: TruthChecker, case: dict[str, Any]) -> CheckReport:
    if case["kind"] == "answer":
        return checker.check_answer(case["question"], case["text"], case["cited"])
    return checker.check_unit(
        case["text"], case["cited"], extra_allowed=case.get("extra_allowed", [])
    )


def evaluate(cases: list[dict[str, Any]], checker: TruthChecker) -> list[Outcome]:
    return [Outcome(c, run_case(checker, c)) for c in cases]


def summarize(outcomes: list[Outcome]) -> dict[str, float]:
    bad = [o for o in outcomes if o.case["expect"] == "block"]
    good = [o for o in outcomes if o.case["expect"] == "pass"]
    caught = sum(o.report.blocked for o in bad)
    false_blocks = sum(o.report.blocked for o in good)
    return {
        "cases": len(outcomes),
        "fabrications": len(bad),
        "caught": caught,
        "catch_rate": caught / len(bad) if bad else 1.0,
        "true_cases": len(good),
        "false_blocks": false_blocks,
        "false_block_rate": false_blocks / len(good) if good else 0.0,
    }


def _facts_path() -> Path:
    # Cases cite example-sheet ids and text, so the eval never reads the private sheet.
    return ROOT / "facts.example.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="all", choices=["tune", "heldout", "all"])
    args = parser.parse_args(argv)
    cases = load_cases(split=args.split)
    sheet = eval_sheet()
    stats = summarize(evaluate(cases, TruthChecker(sheet, model_stub(cases), TODAY)))
    print(f"split={args.split} {stats}")
    # With the model pass failing, rules alone must still catch every literal fabrication.
    literal = [c for c in cases if c["expect"] == "block" and not c.get("needs_model")]
    failing = evaluate(literal, TruthChecker(sheet, FailingLLM(), TODAY))
    rule_only = [o for o in failing if o.report.rule_violations]
    print(f"rules alone catch {len(rule_only)}/{len(literal)} literal fabrications (LLM failing)")
    passing = evaluate(
        [c for c in cases if c["expect"] == "pass" and c["kind"] != "answer"],
        TruthChecker(sheet, FailingLLM(), TODAY),
    )
    print(f"failing LLM blocks {sum(o.report.blocked for o in passing)}/{len(passing)} true lines")
    ok = (
        stats["catch_rate"] == 1.0
        and stats["false_block_rate"] < 0.10
        and len(rule_only) == len(literal)
    )
    for o in evaluate(cases, TruthChecker(sheet, model_stub(cases), TODAY)):
        if not o.correct:
            codes = [(v.code, v.detail) for v in o.report.violations]
            print(
                f"  WRONG {o.case['id']} expect={o.case['expect']} got={o.report.verdict} {codes}"
            )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
