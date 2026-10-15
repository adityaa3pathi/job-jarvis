"""U5: AI result cache, usage log and daily budget, all with the stub LLM."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.llm import GeminiLLM, StubLLM, Usage, ask, usage_sink
from jobagent.resume import coach, jd_agent
from jobagent.resume.coach import build_report
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.match import match_profile
from jobagent.web.store import Store, ist_day
from tests.test_jd import GOOD, JD, router
from tests.test_web import ADVICE, PASSWORD, SUMMARY, client_for, done, fake
from tests.test_web import JD as WEB_JD


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "web.sqlite3")


def test_same_posting_twice_makes_no_second_call(store: Store) -> None:
    llm = router()
    first = analyze_jd(JD, llm, cache=store)
    assert len(llm.calls) == 1
    again = analyze_jd(JD, llm, cache=store)
    assert len(llm.calls) == 1
    assert again == first


def test_prompt_version_change_misses_the_cache(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = router()
    analyze_jd(JD, llm, cache=store)
    monkeypatch.setattr(jd_agent, "PROMPT_VERSION", "jd-test")
    analyze_jd(JD, llm, cache=store)
    assert len(llm.calls) == 2


def test_another_model_misses_the_cache(store: Store) -> None:
    llm, other = router(), router()
    other.model = "other-model"
    analyze_jd(JD, llm, cache=store)
    analyze_jd(JD, other, cache=store)
    assert len(other.calls) == 1


def test_a_degraded_scan_is_never_cached(store: Store) -> None:
    from jobagent.llm import FailingLLM

    assert analyze_jd(JD, FailingLLM(), cache=store).degraded
    llm = router()
    assert not analyze_jd(JD, llm, cache=store).degraded
    assert len(llm.calls) == 1


def test_coach_report_is_cached_by_its_prompt(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = StubLLM(lambda s, u: ADVICE)
    profile = analyze_jd(JD, router())
    coverage = match_profile(profile, cards_from_summary(SUMMARY))
    first = build_report(profile, coverage, SUMMARY, llm, cache=store)
    second = build_report(profile, coverage, SUMMARY, llm, cache=store)
    assert len(llm.calls) == 1 and first == second
    build_report(profile, coverage, SUMMARY + " I also use Go.", llm, cache=store)
    assert len(llm.calls) == 2  # new inputs, new key
    monkeypatch.setattr(coach, "PROMPT_VERSION", "coach-test")
    build_report(profile, coverage, SUMMARY, llm, cache=store)
    assert len(llm.calls) == 3


def test_every_reply_inside_the_sink_is_recorded(store: Store) -> None:
    llm = router()
    with usage_sink(store.add_usage):
        analyze_jd(JD, llm)
    (row,) = store.usage_rows()
    assert row["step"] == "jd_extract" and row["model"] == "stub"
    assert row["tokens_in"] > 0 and row["tokens_out"] > 0
    assert store.usage_today() == {"calls": 1, "tokens": row["tokens_in"] + row["tokens_out"]}


def test_usage_day_is_the_ist_day() -> None:
    assert ist_day(datetime(2026, 10, 15, 19, 0, tzinfo=UTC)) == "2026-10-16"
    assert ist_day(datetime(2026, 10, 15, 18, 0, tzinfo=UTC)) == "2026-10-15"


def gemini_reply(meta: dict[str, int]) -> httpx.Response:
    text = json.dumps(GOOD)
    body = {"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": meta}
    return httpx.Response(200, json=body)


def test_gemini_usage_metadata_and_output_cap_per_step() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return gemini_reply(
            {"promptTokenCount": 900, "candidatesTokenCount": 120, "thoughtsTokenCount": 30,
             "cachedContentTokenCount": 512}
        )  # fmt: skip

    llm = GeminiLLM(
        "m", api_key="k", transport=httpx.MockTransport(handler),
        max_output_tokens={"jd_extract": 777},
    )  # fmt: skip
    seen: list[Usage] = []
    with usage_sink(seen.append):
        analyze_jd(JD, llm)
        ask(llm, jd_agent.Pass, system="s", user="u", step="unlisted")
    assert sent[0]["generationConfig"]["maxOutputTokens"] == 777
    assert "maxOutputTokens" not in sent[1]["generationConfig"]
    assert all("thinkingConfig" not in body["generationConfig"] for body in sent)
    first = seen[0]
    assert (first.step, first.model, first.tokens_in, first.tokens_out) == (
        "jd_extract",
        "m",
        900,
        150,
    )
    assert first.tokens_cached == 512 and first.ms >= 0


def test_thinking_budget_is_sent_only_when_configured() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return gemini_reply({})

    llm = GeminiLLM("m", api_key="k", transport=httpx.MockTransport(handler), thinking_budget=0)
    llm.complete_json(system="s", user="u")
    assert sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert Settings().llm.thinking_budget is None


def test_a_broken_usage_store_does_not_fail_the_call() -> None:
    def broken(_: Usage) -> None:
        raise RuntimeError("db gone")

    with usage_sink(broken):
        assert not analyze_jd(JD, router()).degraded


def counting(settings: Settings) -> tuple[TestClient, StubLLM]:
    llm = StubLLM(fake)
    c = client_for(settings, llm=llm)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    c.put("/api/profile/summary", json={"summary": SUMMARY})
    return c, llm


def test_repeat_coach_on_the_same_post_makes_no_ai_calls(settings: Settings) -> None:
    c, llm = counting(settings)
    first = done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))
    assert first["status"] == "done" and len(llm.calls) == 2  # JD + report
    second = done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))
    assert second["status"] == "done" and len(llm.calls) == 2
    one = c.get(f"/api/coach/{first['result']['session_id']}").json()["report"]
    two = c.get(f"/api/coach/{second['result']['session_id']}").json()["report"]
    assert one == two


def test_ai_tasks_write_usage_rows(settings: Settings) -> None:
    c, _ = counting(settings)
    done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))
    st = Store(settings.db_path)
    try:
        steps = [r["step"] for r in st.usage_rows()]
    finally:
        st.close()
    assert steps == ["jd_extract", "coach_report"]


def test_budget_reached_refuses_new_ai_tasks(settings: Settings) -> None:
    settings.llm.daily_call_budget = 2
    c, llm = counting(settings)
    sid = done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))["result"]["session_id"]
    for res in (
        c.post("/api/coach", json={"jd_text": WEB_JD + " Go."}),
        c.post(f"/api/coach/{sid}/chat", json={"message": "hi"}),
    ):
        assert res.status_code == 429
        assert "Daily AI budget reached" in res.json()["detail"]
        assert "2 of 2 calls" in res.json()["detail"]
    assert len(llm.calls) == 2


def test_token_budget_also_blocks(settings: Settings) -> None:
    settings.llm.daily_token_budget = 1
    c, _ = counting(settings)
    done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))
    res = c.post("/api/coach", json={"jd_text": WEB_JD})
    assert res.status_code == 429 and "tokens" in res.json()["detail"]
