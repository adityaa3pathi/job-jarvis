"""Ranking eval: precision and recall of the fit bar against the owner's labels.

Usage: uv run python evals/run_rank_eval.py [--labels PATH] [--judge rules|gemini]
"""

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

from jobagent.config import load_settings  # noqa: E402
from jobagent.facts import FactSheet, load_facts  # noqa: E402
from jobagent.llm import LLM, GeminiLLM, StubLLM  # noqa: E402
from jobagent.models import Posting  # noqa: E402
from jobagent.rank import rank_posting  # noqa: E402

TODAY = date(2026, 10, 8)
MIN_LABELS = 40


@dataclass
class BarStats:
    bar: int
    precision: float
    recall: float
    f1: float


def rules_only_judge() -> LLM:
    """Gives every posting a 5, so the final score equals the rule ceiling."""
    return StubLLM(lambda s, u: {"score": 5, "reasons": ["rules only"], "track": "ai"})


def load_labelled(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text()) or {}
    return [x for x in data.get("labels", []) if x.get("label") in ("apply", "skip")]


def to_posting(row: dict[str, Any]) -> Posting:
    return Posting(
        source=row.get("source", "label"),
        url=row.get("url") or f"https://example.invalid/{row['id']}",
        company=row["company"],
        title=row["title"],
        location=row.get("location", ""),
        experience_text=row.get("experience_text", ""),
        stack_words=row.get("stack_words", []),
        description=row.get("description", ""),
    )


def score_all(rows: list[dict[str, Any]], sheet: FactSheet, llm: LLM) -> list[int]:
    """Final score per row; dropped or unscored rows count as 0 (never applied)."""
    scores = []
    for row in rows:
        res = rank_posting(to_posting(row), sheet, llm, today=TODAY)
        scores.append(res.score or 0)
    return scores


def stats_at(rows: list[dict[str, Any]], scores: list[int], bar: int) -> BarStats:
    actual = [r["label"] == "apply" for r in rows]
    predicted = [s >= bar for s in scores]
    tp = sum(a and p for a, p in zip(actual, predicted, strict=True))
    fp = sum((not a) and p for a, p in zip(actual, predicted, strict=True))
    fn = sum(a and (not p) for a, p in zip(actual, predicted, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return BarStats(bar, precision, recall, f1)


def best_bar(table: list[BarStats]) -> int:
    return max(table, key=lambda s: (s.f1, s.bar)).bar


def _facts_path() -> Path:
    real = ROOT / "facts.yaml"
    return real if real.exists() else ROOT / "facts.example.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, default=ROOT / "evals" / "labels.yaml")
    parser.add_argument("--judge", choices=["rules", "gemini"], default="rules")
    args = parser.parse_args(argv)
    settings = load_settings()
    rows = load_labelled(args.labels)
    if not rows:
        print(f"No labelled rows in {args.labels}. Set label: apply or skip on 40 to 60 rows.")
        return 0
    sheet = load_facts(_facts_path())
    llm: LLM = rules_only_judge() if args.judge == "rules" else GeminiLLM(settings.llm.model)
    scores = score_all(rows, sheet, llm)
    table = [stats_at(rows, scores, bar) for bar in range(1, 6)]
    print(f"judge={args.judge} labelled={len(rows)} configured bar={settings.fit_bar}")
    if len(rows) < MIN_LABELS:
        print(f"WARNING: only {len(rows)} labels, need {MIN_LABELS} to 60 for a trustworthy bar")
    for s in table:
        mark = " <- configured" if s.bar == settings.fit_bar else ""
        print(f"bar {s.bar}: precision {s.precision:.2f} recall {s.recall:.2f} f1 {s.f1:.2f}{mark}")
    print(f"suggested bar: {best_bar(table)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
