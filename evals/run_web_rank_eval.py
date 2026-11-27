"""Ranking eval for the web path (stage 2): `score_found` plus `judge_top`, as the daily
run ranks found jobs, scored against the owner's yes/no labels on a fixed pool.

The pool comes from evals/export_label_pool.py: 100 found jobs, stratified by the live
score at export time (the web's top 30, then random samples of 80+, 60-79 and under 60)
with each stratum's population size. Labels live outside the repo, which is public.

Every posting goes through the product's own save step (`Database.upsert_many`), so the
hard rules and derived fields (years, role, work mode) come from the current code. A job
the hard rules drop counts as not recommended. Nothing is read from or written to Turso:
the run refuses to start while TURSO_DATABASE_URL is set.

Metrics (fit bar = `fit_bar` x 20 = 80 on the web's 0-100 scale, the judge's cap scale):
  p_at_20, p_at_50  share of yes among the pool's top k by final score. Ties are counted
                    fairly: a tie group cut by k adds its own yes rate for the slots left.
                    Pooled: only the 100 labelled jobs are ranked, not all found jobs.
  recall_bar        share of yes jobs scoring 80+, weighted back to every found job by
                    stratum size (population / sampled), with a 90% stratified bootstrap
                    interval. The under-60 stratum has weight ~900, so it is wide.
  precision_bar     share of jobs scoring 80+ that are yes, weighted the same way.
  auc               chance a random yes job outscores a random no job (ties count half),
                    inside the pool. Needs no bar, so it still moves when no job reaches 80.

Usage (no model; rules plus the local embedding model):
  uv run python -m evals.run_web_rank_eval --pool ~/job-jarvis-labels/label-pool-2026-11-27
  ... --judge live     the AI judge on the top `judge_top_n` (spends quota; owner only)
  ... --scores served  score the stored live scores from the export instead of re-ranking
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from jobagent.config import load_settings
from jobagent.db import Database
from jobagent.index import Embedder, FastEmbedEmbedder, HashingEmbedder
from jobagent.llm import LLM
from jobagent.models import Posting
from jobagent.rank import JudgeSummary, judge_top, score_found
from jobagent.web.store import Store

STRATA = ("top", "band80", "band60", "low")
BOOT = 2000


@dataclass
class Item:
    job_id: int  # the production id, from the export
    stratum: str
    yes: bool
    sure: bool
    score: int  # final score; -1 when the hard rules dropped the job
    title: str = ""


@dataclass
class RankEvalResult:
    mode: str
    p_at_20: float
    p_at_50: float
    recall_bar: float
    recall_ci: tuple[float, float]
    precision_bar: float
    precision_ci: tuple[float, float]
    pool_recall_bar: float  # unweighted, inside the pool only
    auc: float
    labels: int
    yes: int
    unsure: int
    dropped: int
    unsure_in_top20: int
    judge: JudgeSummary | None = None
    top: list[Item] = field(default_factory=list)


def load_pool(stem: Path) -> tuple[dict[str, Any], dict[int, dict[str, str]]]:
    """The export snapshot (`<stem>.json`) and its labels (`<stem>.csv`, yes/no)."""
    snap = json.loads(stem.with_suffix(".json").read_text())
    with stem.with_suffix(".csv").open(newline="") as fh:
        labels = {int(r["job_id"]): r for r in csv.DictReader(fh)}
    bad = [i for i, r in labels.items() if r["label"].strip().lower() not in ("yes", "no")]
    if bad:
        raise SystemExit(f"{len(bad)} rows have no yes/no label, e.g. job_id {bad[0]}")
    return snap, labels


def to_posting(job: dict[str, Any]) -> Posting:
    posted = str(job.get("posted_date") or "")[:10]
    return Posting(
        source=job["source"], url=job["url"] or f"https://example.invalid/{job['id']}",
        company=job["company"], title=job["title"], location=job["location"] or "",
        experience_text=job["experience_text"] or "",
        stack_words=json.loads(job["stack_words"] or "[]"),
        posted_date=date.fromisoformat(posted) if len(posted) == 10 else None,
        description=job["description"] or "",
    )  # fmt: skip


def build_db(
    path: Path, snap: dict[str, Any], max_years_asked: int
) -> tuple[Database, dict[str, dict[str, Any]]]:
    """A fresh local DB holding only the pool, the profile and the owner's real marks
    (the pool labels are never fed to the preference model). Returns db and url -> job."""
    db = Database(path, max_years_asked)
    Store(path)  # creates the profile table
    profile = snap["profile"][0]
    marks = snap["marks"]
    with db.conn:
        db.conn.execute(
            "INSERT INTO profile(id, summary, base_tex, updated_at) VALUES (1, ?, ?, ?)",
            (profile["summary"], profile.get("base_tex") or "", profile["updated_at"]),
        )
        db.conn.executemany(
            "INSERT INTO labels(job_id, label, status, role, source, company, work_mode, at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    -m["job_id"],
                    m["label"],
                    m["status"],
                    m["role"],
                    m["source"],
                    m["company"],
                    m["work_mode"],
                    m["at"],
                )
                for m in marks
            ],  # fmt: skip
        )
    postings = [to_posting(j) for j in snap["jobs"]]
    db.upsert_many(postings)
    return db, {p.url: j for p, j in zip(postings, snap["jobs"], strict=True)}


def final_scores(db: Database, by_url: dict[str, dict[str, Any]]) -> dict[int, int]:
    rows = db.conn.execute(
        "SELECT j.status, j.score, l.url FROM jobs j JOIN job_links l ON l.job_id = j.id"
    ).fetchall()
    out: dict[int, int] = {}
    for status, score, url in rows:
        job = by_url.get(url)
        if job is not None:
            out[job["id"]] = -1 if status != "found" or score is None else int(score)
    missing = [j["id"] for j in by_url.values() if j["id"] not in out]
    if missing:
        raise RuntimeError(f"{len(missing)} pool jobs were not saved, e.g. {missing[0]}")
    return out


def p_at_k(items: Sequence[Item], k: int) -> float:
    """Expected precision of the top k when ties break at random."""
    ranked = sorted(items, key=lambda i: -i.score)
    k = min(k, len(ranked))
    if k == 0:
        return 0.0
    cut = ranked[k - 1].score
    above = [i for i in ranked if i.score > cut]
    tied = [i for i in ranked if i.score == cut]
    rate = sum(i.yes for i in tied) / len(tied)
    return (sum(i.yes for i in above) + (k - len(above)) * rate) / k


def auc(items: Sequence[Item]) -> float:
    yes = [i.score for i in items if i.yes]
    no = [i.score for i in items if not i.yes]
    if not yes or not no:
        return 0.0
    wins = sum((y > n) + 0.5 * (y == n) for y in yes for n in no)
    return wins / (len(yes) * len(no))


def _weights(snap: dict[str, Any]) -> dict[str, float]:
    s = snap["meta"]["strata"]
    return {name: s[name]["population"] / s[name]["sampled"] for name in STRATA}


def _at_bar(items: Sequence[Item], w: dict[str, float], bar: int) -> tuple[float, float]:
    yes = sum(w[i.stratum] for i in items if i.yes)
    hit = sum(w[i.stratum] for i in items if i.yes and i.score >= bar)
    shown = sum(w[i.stratum] for i in items if i.score >= bar)
    return (hit / yes if yes else 0.0), (hit / shown if shown else 0.0)


def _interval(xs: list[float]) -> tuple[float, float]:
    ordered = sorted(xs)
    return ordered[int(0.05 * len(ordered))], ordered[int(0.95 * len(ordered)) - 1]


def _boot(
    items: Sequence[Item], w: dict[str, float], bar: int, seed: int = 7
) -> list[tuple[float, float]]:
    """90% intervals for weighted recall and precision: resample within each stratum."""
    rng = random.Random(seed)
    groups = {s: [i for i in items if i.stratum == s] for s in STRATA}
    rec: list[float] = []
    prec: list[float] = []
    for _ in range(BOOT):
        sample = [rng.choice(g) for g in groups.values() for _ in g]
        r, p = _at_bar(sample, w, bar)
        rec.append(r)
        prec.append(p)
    return [_interval(rec), _interval(prec)]


def score_items(
    snap: dict[str, Any], labels: dict[int, dict[str, str]], scores: dict[int, int],
    bar: int, mode: str, judge: JudgeSummary | None = None,
) -> RankEvalResult:  # fmt: skip
    items = [
        Item(j["id"], j["stratum"], labels[j["id"]]["label"].strip().lower() == "yes",
             labels[j["id"]].get("sure", "sure") == "sure", scores[j["id"]], j["title"])
        for j in snap["jobs"]
    ]  # fmt: skip
    w = _weights(snap)
    recall, precision = _at_bar(items, w, bar)
    (rlo, rhi), (plo, phi) = _boot(items, w, bar)
    yes = [i for i in items if i.yes]
    ranked = sorted(items, key=lambda i: -i.score)
    return RankEvalResult(
        mode=mode, p_at_20=p_at_k(items, 20), p_at_50=p_at_k(items, 50),
        recall_bar=recall, recall_ci=(rlo, rhi), precision_bar=precision,
        precision_ci=(plo, phi),
        pool_recall_bar=sum(i.score >= bar for i in yes) / len(yes) if yes else 0.0,
        auc=auc(items),
        labels=len(items), yes=len(yes), unsure=sum(not i.sure for i in items),
        dropped=sum(i.score < 0 for i in items),
        unsure_in_top20=sum(not i.sure for i in ranked[:20]), judge=judge, top=ranked[:20],
    )  # fmt: skip


def load_embedder(name: str) -> Embedder | None:
    if name == "none":
        return None
    if name == "hashing":
        return HashingEmbedder()
    return FastEmbedEmbedder()


def run(
    stem: Path, *, scores: str = "pipeline", embedder: str = "fastembed",
    llm: LLM | None = None, cache: Path | None = None, top_n: int | None = None,
) -> RankEvalResult:  # fmt: skip
    if os.environ.get("TURSO_DATABASE_URL"):
        raise SystemExit("TURSO_DATABASE_URL is set: this eval must never touch Turso")
    settings = load_settings()
    bar = settings.fit_bar * 20
    snap, labels = load_pool(stem)
    if scores == "served":
        served = {j["id"]: int(j["score"]) for j in snap["jobs"]}
        return score_items(snap, labels, served, bar, "served at export")
    with tempfile.TemporaryDirectory() as tmp:
        db, by_url = build_db(Path(tmp) / "eval.sqlite3", snap, settings.max_years_asked)
        model = load_embedder(embedder)
        score_found(db, model, owner_years=settings.ranking.owner_years)
        summary = None
        mode = f"pipeline ({embedder}"
        if llm is not None:
            st = Store(cache or Path(tmp) / "judge-cache.sqlite3")
            n = settings.ranking.judge_top_n if top_n is None else top_n
            summary = judge_top(db, st, llm, top_n=n, budget=settings.llm)
            mode += f" + judge top {n}"
        result = score_items(snap, labels, final_scores(db, by_url), bar, mode + ")", summary)
        db.close()
    return result


def describe(r: RankEvalResult) -> str:
    lines = [
        f"mode            {r.mode}",
        f"labels          {r.labels} ({r.yes} yes, {r.unsure} unsure; labelled by Claude)",
        f"p_at_20         {r.p_at_20:.3f}  ({r.unsure_in_top20} of the top 20 are unsure labels)",
        f"p_at_50         {r.p_at_50:.3f}",
        f"recall_bar      {r.recall_bar:.3f}  90% CI {r.recall_ci[0]:.3f}-{r.recall_ci[1]:.3f}"
        f"  (pool, unweighted: {r.pool_recall_bar:.3f})",
        f"precision_bar   {r.precision_bar:.3f}  90% CI"
        f" {r.precision_ci[0]:.3f}-{r.precision_ci[1]:.3f}",
        f"auc             {r.auc:.3f}",
        f"dropped         {r.dropped} by the hard rules",
    ]
    if r.judge is not None:
        j = r.judge
        lines.append(f"judge           judged {j.judged} ({j.calls} calls, {j.cache_hits} cached)"
                     + ("; FAILED" if j.failed else "")
                     + ("; budget reached" if j.stopped_by_budget else ""))  # fmt: skip
    lines.append("top 20:")
    lines += [f"  {i.score:>3}  {'yes' if i.yes else 'no ':3}  {i.stratum:6}  {i.title[:60]}"
              for i in r.top]  # fmt: skip
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=Path, required=True, help="path without .csv/.json")
    ap.add_argument("--scores", choices=("pipeline", "served"), default="pipeline")
    ap.add_argument("--embedder", choices=("fastembed", "hashing", "none"), default="fastembed")
    ap.add_argument("--judge", choices=("none", "live"), default="none")
    ap.add_argument("--cache", type=Path, help="judge cache DB, kept between runs")
    ap.add_argument("--top-n", type=int)
    args = ap.parse_args(argv)
    llm = None
    if args.judge == "live":
        from jobagent.llm import GeminiLLM

        llm = GeminiLLM.from_settings(load_settings().llm, 0.0)
    stem = args.pool.expanduser()
    result = run(stem, scores=args.scores, embedder=args.embedder, llm=llm,
                 cache=args.cache, top_n=args.top_n)  # fmt: skip
    print(describe(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
