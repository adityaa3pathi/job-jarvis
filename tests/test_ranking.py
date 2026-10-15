"""Tiered ranking (U14): cheap score on every found job, labels, AI judge on the top N."""

from __future__ import annotations

from typing import Any

import pytest

from jobagent.config import LLMSettings, Settings
from jobagent.db import Database, today_ist
from jobagent.index import HashingEmbedder
from jobagent.llm import FailingLLM, StubLLM
from jobagent.models import Posting
from jobagent.rank import Preferences, cheap_score, judge_top, profile_ceiling, score_found
from jobagent.web.store import Store

PROFILE = "AI engineer. I build LangGraph agents, RAG with pgvector, FastAPI and Python APIs."
GOOD = "Build LangGraph agents with RAG, pgvector, FastAPI and Python. Ship MCP servers."
BAD = "Maintain Java Spring and Kotlin services on Oracle. SAP ABAP a plus. Cobol mainframe."


class CountingEmbedder(HashingEmbedder):
    name = "fake-embedder"

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts += texts
        return super().embed(texts)


def job(i: int, description: str, **kw: Any) -> Posting:
    base: dict[str, Any] = {
        "source": "greenhouse", "url": f"https://x.example/{i}", "company": f"Co{i}",
        "title": f"AI Engineer {i}", "location": "Bengaluru",
        "description": f"{description} Ref {i}.",  # unique: no merge, no shared cache entry
    }  # fmt: skip
    return Posting.model_validate(base | kw)


def db_with(settings: Settings, postings: list[Posting], profile: str = PROFILE) -> Database:
    st = Store(settings.db_path)
    st.save_profile(summary=profile)
    st.close()
    db = Database(settings.db_path)
    db.upsert_many(postings)
    return db


def scores(db: Database) -> dict[int, tuple[int | None, str]]:
    rows = db.conn.execute("SELECT id, score, rank_reason FROM jobs").fetchall()
    return {r["id"]: (r["score"], r["rank_reason"]) for r in rows}


def test_profile_ceiling_follows_stack_overlap() -> None:
    high, why = profile_ceiling(PROFILE, GOOD, None, None)
    low, _ = profile_ceiling(PROFILE, BAD, None, None)
    assert high > low and "of" in why[0]
    capped, why = profile_ceiling(PROFILE, GOOD, 6, 2)
    assert capped < high and "years" in why[-1]


def test_cheap_score_never_passes_the_rule_ceiling() -> None:
    assert cheap_score(2, 1.0, 1.0) <= 40
    assert 0 <= cheap_score(1, None, 0.0) <= cheap_score(5, None, 0.5) <= 100


def test_better_matching_job_scores_higher(settings: Settings) -> None:
    db = db_with(settings, [job(1, BAD), job(2, GOOD)])
    emb = CountingEmbedder()
    summary = score_found(db, emb)
    bad, good = (scores(db)[i] for i in (1, 2))
    assert summary.scored == 2
    assert good[0] is not None and bad[0] is not None and good[0] > bad[0]
    assert "profile match" in good[1]
    best = db.query_jobs(db_filters("best"), limit=10, offset=0)[0]
    assert [r["id"] for r in best] == [2, 1]


def test_embeddings_are_cached_by_text_hash(settings: Settings) -> None:
    db = db_with(settings, [job(1, GOOD), job(2, BAD)])
    emb = CountingEmbedder()
    score_found(db, emb)
    first = len(emb.texts)
    score_found(db, emb)
    assert first == 3 and len(emb.texts) == first  # two postings + the profile, once


def test_no_embedder_falls_back_to_rules_only(settings: Settings) -> None:
    db = db_with(settings, [job(1, GOOD), job(2, BAD)])
    score_found(db, None)
    got = scores(db)
    assert got[1][0] is not None and got[1][0] > (got[2][0] or 0)
    assert "profile match" not in got[1][1]


def test_labels_change_the_preference_term(settings: Settings) -> None:
    db = db_with(settings, [
        job(1, GOOD, company="Liked"), job(2, GOOD, company="Liked"),
        job(3, GOOD, company="Hated"), job(4, GOOD, company="Hated"),
    ])  # fmt: skip
    score_found(db, None)
    before = scores(db)
    assert before[2][0] == before[4][0]
    db.mark(1, "applied")
    db.mark(3, "dropped")
    labels = db.labels()
    assert {(r["job_id"], r["label"]) for r in labels} == {(1, 1), (3, 0)}
    prefs = Preferences.fit(labels)
    liked, _ = prefs.predict({"company": "Liked", "role": "ai_ml", "source": "x", "work_mode": ""})
    hated, _ = prefs.predict({"company": "Hated", "role": "ai_ml", "source": "x", "work_mode": ""})
    assert liked > 0.5 > hated
    score_found(db, None)
    after = scores(db)
    assert (after[2][0] or 0) > (after[4][0] or 0)
    db.mark(1, "found")  # undoing a mark removes its label
    assert {r["job_id"] for r in db.labels()} == {3}


def test_interview_is_positive_and_rejected_keeps_the_label(settings: Settings) -> None:
    db = db_with(settings, [job(1, GOOD)])
    db.mark(1, "interview")
    db.mark(1, "rejected")
    assert [(r["job_id"], r["label"]) for r in db.labels()] == [(1, 1)]


def db_filters(sort: str) -> Any:
    from jobagent.models import JobFilters

    return JobFilters.model_validate({"sort": sort})


def judge_reply(score: int) -> StubLLM:
    return StubLLM(lambda s, u: {"score": score, "reasons": ["needs Go"], "track": "ai"})


def test_judge_runs_only_on_the_top_n_and_only_lowers(settings: Settings) -> None:
    db = db_with(settings, [job(i, GOOD if i < 3 else BAD) for i in range(1, 6)])
    score_found(db, CountingEmbedder())
    before = scores(db)
    top = sorted(before, key=lambda i: -(before[i][0] or 0))[:2]
    st, llm = Store(settings.db_path), judge_reply(1)
    out = judge_top(db, st, llm, top_n=2, budget=LLMSettings())
    assert (out.judged, out.calls) == (2, 2) and len(llm.calls) == 2
    after = scores(db)
    for i in top:
        assert (after[i][0] or 0) <= 20 < (before[i][0] or 0)
        assert "AI judge: needs Go" in after[i][1]
    assert all(after[i] == before[i] for i in before if i not in top)
    # The daily cap is spent: a second pass the same day judges nothing.
    assert judge_top(db, st, llm, top_n=2, budget=LLMSettings()).judged == 0
    # A rescore keeps the judge's lower value.
    score_found(db, None)
    assert all((scores(db)[i][0] or 0) <= 20 for i in top)


def test_judge_never_raises_the_score(settings: Settings) -> None:
    db = db_with(settings, [job(1, BAD)])
    score_found(db, None)
    before = scores(db)[1]
    judge_top(db, Store(settings.db_path), judge_reply(5), top_n=5, budget=LLMSettings())
    assert scores(db)[1][0] == before[0]


def test_judge_stops_at_the_daily_budget(settings: Settings) -> None:
    db = db_with(settings, [job(i, GOOD) for i in range(1, 4)])
    score_found(db, None)
    st, llm = Store(settings.db_path), judge_reply(2)
    out = judge_top(db, st, llm, top_n=30, budget=LLMSettings(daily_call_budget=1))
    assert (out.calls, out.stopped_by_budget) == (1, True)
    assert len(st.usage_rows()) == 1 and st.usage_rows()[0]["step"] == "rank_judge"


def test_judge_cache_hit_makes_no_call(settings: Settings) -> None:
    db = db_with(settings, [job(1, GOOD)])
    score_found(db, None)
    st = Store(settings.db_path)
    judge_top(db, st, judge_reply(2), top_n=5, budget=LLMSettings())
    db.conn.execute("UPDATE jobs SET judged_on=NULL, judge_score=NULL")
    db.conn.commit()
    again = judge_reply(2)
    out = judge_top(db, st, again, top_n=5, budget=LLMSettings())
    assert (out.judged, out.calls, out.cache_hits, len(again.calls)) == (1, 0, 1, 0)


def test_failed_judge_leaves_jobs_for_the_next_run(settings: Settings) -> None:
    db = db_with(settings, [job(1, GOOD), job(2, GOOD)])
    score_found(db, None)
    llm = FailingLLM()
    out = judge_top(db, Store(settings.db_path), llm, top_n=5, budget=LLMSettings())
    assert (out.judged, out.failed, llm.calls) == (0, True, 2)  # one job, one retry, then stop
    assert db.judged_on(today_ist().isoformat()) == 0


def test_judge_skipped_without_a_profile(settings: Settings) -> None:
    db = db_with(settings, [job(1, GOOD)], profile="")
    score_found(db, None)
    llm = judge_reply(2)
    assert judge_top(db, Store(settings.db_path), llm, top_n=5, budget=LLMSettings()).judged == 0
    assert llm.calls == []


@pytest.mark.parametrize("n", [3, 7])
def test_best_sort_pages_are_stable_with_ties(settings: Settings, n: int) -> None:
    db = db_with(settings, [job(i, GOOD if i % 2 else BAD) for i in range(1, 12)])
    score_found(db, None)
    walked: list[int] = []
    for offset in range(0, 11, n):
        walked += [r["id"] for r in db.query_jobs(db_filters("best"), limit=n, offset=offset)[0]]
    assert sorted(walked) == list(range(1, 12)) and len(set(walked)) == 11
    got = scores(db)
    assert [got[i][0] for i in walked] == sorted((got[i][0] for i in walked), reverse=True)


def test_embedder_loads_once_per_process_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    from jobagent import index

    loads: list[int] = []

    def slow_load() -> HashingEmbedder:
        loads.append(1)
        time.sleep(0.05)
        return HashingEmbedder()

    monkeypatch.setattr(index, "load_embedder", slow_load)
    monkeypatch.setattr(index, "_shared", [])
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(lambda _: index.shared_embedder(), range(8)))
    assert len(loads) == 1 and all(g is got[0] for g in got)
    assert index.shared_embedder() is got[0]
