"""The web ranking eval's maths and safety guard, on a synthetic pool (no real labels)."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from evals.run_web_rank_eval import Item, auc, p_at_k, run, score_items


def _item(score: int, yes: bool, stratum: str = "top") -> Item:
    return Item(job_id=score, stratum=stratum, yes=yes, sure=True, score=score)


def test_p_at_k_counts_a_cut_tie_group_at_its_own_yes_rate() -> None:
    items = [_item(90, True), *[_item(80, i < 2) for i in range(4)], _item(10, True)]
    # top 3 = the 90 (yes) plus 2 of the four tied 80s, whose yes rate is 0.5
    assert p_at_k(items, 3) == pytest.approx((1 + 2 * 0.5) / 3)
    assert p_at_k(items, 1) == 1.0


def test_auc_is_half_when_scores_cannot_tell_jobs_apart() -> None:
    assert auc([_item(50, True), _item(50, False)]) == 0.5
    assert auc([_item(60, True), _item(40, False)]) == 1.0


def _pool(tmp_path: Path) -> Path:
    jobs, rows = [], []
    for n in range(8):
        stratum = "top" if n < 4 else "low"
        ai = n % 2 == 0
        jobs.append({
            "id": n + 1, "stratum": stratum, "source": "lever", "company": f"Co{n}",
            "title": "AI Engineer" if ai else "Accountant", "location": "Bengaluru",
            "experience_text": "", "stack_words": json.dumps(["Python"] if ai else []),
            "posted_date": None, "url": f"https://example.invalid/{n}", "score": 50,
            "description": "Python FastAPI LangGraph RAG" if ai else "Ledgers and tax filing",
        })  # fmt: skip
        rows.append({"n": n + 1, "label": "yes" if ai else "no", "sure": "sure",
                     "job_id": n + 1})  # fmt: skip
    snap = {
        "meta": {
            "strata": {
                s: {"sampled": 4, "population": p}
                for s, p in (("top", 4), ("band80", 1), ("band60", 1), ("low", 40))
            }
        },  # fmt: skip
        "profile": [
            {"summary": "AI engineer: Python, FastAPI, LangGraph, RAG.", "updated_at": "2026-11-27"}
        ],  # fmt: skip
        "marks": [],
        "jobs": jobs,
    }
    stem = tmp_path / "pool"
    stem.with_suffix(".json").write_text(json.dumps(snap))
    with stem.with_suffix(".csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, ["n", "label", "sure", "job_id"])
        w.writeheader()
        w.writerows(rows)
    return stem


def test_recall_is_weighted_by_stratum_size(tmp_path: Path) -> None:
    snap = json.loads(_pool(tmp_path).with_suffix(".json").read_text())
    labels = {j["id"]: {"label": "yes", "sure": "sure"} for j in snap["jobs"]}
    # every job is yes; the 4 top jobs pass the bar, the 4 low ones (weight 10 each) don't
    scores = {j["id"]: 90 if j["stratum"] == "top" else 10 for j in snap["jobs"]}
    result = score_items(snap, labels, scores, 80, "test")
    assert result.pool_recall_bar == 0.5
    assert result.recall_bar == pytest.approx(4 / (4 + 4 * 10))


def test_pipeline_ranks_the_pool_in_a_local_db(tmp_path: Path) -> None:
    result = run(_pool(tmp_path), embedder="hashing")
    assert result.labels == 8 and result.yes == 4
    assert result.auc > 0.5  # AI jobs match the AI profile better than accountancy


def test_refuses_to_run_against_turso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    with pytest.raises(SystemExit, match="Turso"):
        run(_pool(tmp_path), embedder="hashing")


def test_frozen_posting_fixture_loads_as_postings() -> None:
    import yaml

    from jobagent.models import Posting

    path = Path(__file__).resolve().parents[1] / "evals/fixtures/postings-2026-11-27.yaml"
    rows = yaml.safe_load(path.read_text())["postings"]
    postings = [Posting(**{k: v for k, v in r.items() if k != "id"}) for r in rows]
    assert len(postings) == 30
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    assert all(len(p.description) >= 1000 and not email.search(p.description) for p in postings)
