"""Owner profile facts (U13) and coach resilience and chat lookups (U17)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.llm import FailingLLM, StubLLM
from jobagent.resume.coach import (
    MAX_TOOL_ROUNDS,
    ChatTools,
    Message,
    build_report,
    fit_ceiling,
    reply,
)
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.match import match_profile
from jobagent.web.store import Store
from tests.test_tailor_cli import SAMPLE
from tests.test_web import ADVICE, JD, PASSWORD, SUMMARY, client_for, done, fake, start_session

FACT = "I used Terraform at Acme"


def login(c: TestClient) -> TestClient:
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    c.put("/api/profile/summary", json={"summary": SUMMARY})
    return c


def chat(c: TestClient, sid: int, message: str = "What am I missing?") -> dict[str, Any]:
    state = done(c, c.post(f"/api/coach/{sid}/chat", json={"message": message}))
    assert state["status"] == "done", state
    return state


# --- U13: one owner profile ---


def test_fact_confirmed_in_one_session_is_used_by_the_next_build_and_report(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = login(client_for(settings))
    c.post("/api/profile/tex", files={"file": ("r.tex", SAMPLE.encode(), "text/plain")})
    first = start_session(c)
    chat(c, first)
    (fact,) = c.get(f"/api/coach/{first}").json()["facts"]
    assert c.post(f"/api/coach/{first}/facts/{fact['id']}", json={"confirmed": True}).is_success

    second = start_session(c, company="Beta")
    report = c.get(f"/api/coach/{second}").json()["report"]
    assert "Terraform" in report["proven"] and report["missing_must"] == []
    seen: list[str] = []

    def spy(jd: str, tex: str, cards: Any, *a: Any, **kw: Any) -> Any:
        seen.extend(card.text for card in cards)
        raise RuntimeError("stop after reading the cards")

    monkeypatch.setattr("jobagent.web.app.run_tailor", spy)
    done(c, c.post(f"/api/coach/{second}/resume"))
    assert FACT in seen


def test_pending_facts_are_never_used(settings: Settings) -> None:
    c = login(client_for(settings))
    first = start_session(c)
    chat(c, first)
    second = start_session(c)
    assert c.get(f"/api/coach/{second}").json()["report"]["missing_must"] == ["Terraform"]


def test_owner_lists_and_removes_profile_facts(settings: Settings) -> None:
    c = login(client_for(settings))
    sid = start_session(c, company="Acme")
    chat(c, sid)
    (fact,) = c.get("/api/profile/facts").json()
    assert fact["text"] == FACT and fact["confirmed"] is False
    assert fact["source_session_id"] == sid and fact["type"] == "claim" and fact["created_at"]
    tag = c.get("/api/profile/facts").headers["etag"]
    assert c.delete(f"/api/profile/facts/{fact['id']}").json() == {"ok": True}
    assert c.get("/api/profile/facts").json() == []
    assert c.get(f"/api/coach/{sid}").json()["facts"] == []
    assert c.get("/api/profile/facts").headers["etag"] != tag
    assert c.delete(f"/api/profile/facts/{fact['id']}").status_code == 404


def test_migration_moves_old_session_facts_into_the_profile(tmp_path: Path) -> None:
    path = tmp_path / "w.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE coach_sessions (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', jd_text TEXT NOT NULL,
          profile_json TEXT NOT NULL, report_json TEXT NOT NULL);
        CREATE TABLE facts (
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES coach_sessions(id),
          text TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
        INSERT INTO coach_sessions VALUES (7, '2026-10-01', '', 'AI', 'Acme', 'jd', '{}', '{}');
        INSERT INTO facts(session_id, text, confirmed, at) VALUES
          (7, 'I led the Go rewrite', 1, '2026-10-02T10:00:00+00:00'),
          (7, 'I like Rust', 0, '2026-10-02T11:00:00+00:00');
        """
    )
    raw.commit()
    raw.close()
    st = Store(path)
    try:
        rows = st.profile_facts()
        assert [(r["text"], r["confirmed"], r["source_session_id"]) for r in rows] == [
            ("I led the Go rewrite", True, 7),
            ("I like Rust", False, 7),
        ]
        assert rows[0]["created_at"] == "2026-10-02T10:00:00+00:00"
        assert st.confirmed_facts() == ["I led the Go rewrite"]
        assert st.data_version() > 0
    finally:
        st.close()
    again = Store(path)  # a second start moves nothing twice
    try:
        assert len(again.profile_facts()) == 2
    finally:
        again.close()


# --- U17: degraded report and bounded fit ---


def test_ai_failure_gives_a_rules_only_report_marked_degraded(settings: Settings) -> None:
    c = login(client_for(settings, llm=FailingLLM()))
    state = done(c, c.post("/api/coach", json={"jd_text": JD}))
    assert state["status"] == "done", state
    report = c.get(f"/api/coach/{state['result']['session_id']}").json()["report"]
    assert report["degraded"] is True and report["degraded_reason"]
    assert "Python" in report["proven"]
    assert "Terraform" in report["missing_must"] + report["missing_nice"]
    assert report["learn"][0]["topic"] == "Terraform"
    assert any("Terraform" in line and "interview" in line for line in report["risks"])
    assert 1 <= report["fit"] <= 5 and report["fit_reason"]


def coverage_for(summary: str) -> tuple[Any, Any]:
    from tests.test_ai_calls import router

    profile = analyze_jd(JD, router())
    return profile, match_profile(profile, cards_from_summary(summary))


def test_model_fit_above_the_rule_ceiling_is_clamped() -> None:
    profile, coverage = coverage_for("I write Python.")
    ceiling, why = fit_ceiling(coverage)
    assert ceiling < 5
    report = build_report(profile, coverage, "D", StubLLM(lambda s, u: {**ADVICE, "fit": 5}))
    assert report.fit == ceiling and report.fit_reason == why
    low = {**ADVICE, "fit": 1, "fit_reason": "No production Terraform."}
    report = build_report(profile, coverage, "D", StubLLM(lambda s, u: low))
    assert report.fit == 1 and report.fit_reason == "No production Terraform."


def test_report_prompt_names_the_ceiling() -> None:
    profile, coverage = coverage_for("I write Python.")
    llm = StubLLM(lambda s, u: ADVICE)
    build_report(profile, coverage, "D", llm)
    match = json.loads(llm.calls[0][1].split('<data name="MATCH">\n')[1].split("\n</data>")[0])
    assert match["fit_max"] == fit_ceiling(coverage)[0]


# --- U17: chat lookups ---


def tools() -> ChatTools:
    return ChatTools(
        facts=["I build APIs in Python.", "I trace LLM apps with Langfuse.", FACT],
        posting="Senior AI Engineer. Python and Terraform are required. Langfuse is nice.",
        sessions=[{"id": 3, "title": "ML Engineer", "company": "Gamma", "created_at": "x"}],
    )


def scripted(*answers: dict[str, Any]) -> StubLLM:
    queue = list(answers)
    return StubLLM(lambda s, u: queue.pop(0) if len(queue) > 1 else queue[0])


def run_chat(llm: StubLLM, message: str = "Do I know Terraform?") -> Any:
    profile, coverage = coverage_for(SUMMARY)
    report = build_report(profile, coverage, "D", StubLLM(lambda s, u: ADVICE))
    return reply(report, "D", [Message("user", "hi")], message, llm, job=profile, tools=tools())


def lookup(name: str, arg: str) -> dict[str, Any]:
    return {"lookup": [{"name": name, "arg": arg}]}


def results(user: str) -> list[dict[str, Any]]:
    blocks = user.split('<data name="LOOKUP_RESULTS">\n')[1:]
    return [item for b in blocks for item in json.loads(b.split("\n</data>")[0])]


def test_chat_lookup_round_trip_wraps_results_as_data() -> None:
    llm = scripted(
        {"lookup": [{"name": "find_facts", "arg": "Terraform"},
                    {"name": "quote_posting", "arg": "terraform required"},
                    {"name": "past_sessions", "arg": "Gamma"}]},
        {"reply": "Yes: you used Terraform at Acme.", "new_facts": []},
    )  # fmt: skip
    turn = run_chat(llm)
    assert turn.reply == "Yes: you used Terraform at Acme."
    assert llm.steps == ["coach_chat", "coach_chat"]
    assert "lookup" in llm.calls[0][1].rsplit("</data>", 1)[1]  # offered after the data
    found, quoted, past = results(llm.calls[1][1])
    assert found["result"][0] == FACT
    assert quoted["result"] == ["Python and Terraform are required."]
    assert past["result"] == [{"id": 3, "title": "ML Engineer", "company": "Gamma", "date": "x"}]


def test_propose_fact_only_makes_a_pending_fact(settings: Settings) -> None:
    def respond(system: str, user: str) -> Any:
        if "latest message" in user and "LOOKUP_RESULTS" not in user:
            return lookup("propose_fact", "I ran Terraform in production at Acme")
        if "latest message" in user:
            return {"reply": "Noted; please confirm it on screen.", "new_facts": []}
        return fake(system, user)

    c = login(client_for(settings, llm=StubLLM(respond)))
    sid = start_session(c)
    chat(c, sid, "I ran Terraform in production at Acme")
    (fact,) = c.get(f"/api/coach/{sid}").json()["facts"]
    assert fact["text"] == "I ran Terraform in production at Acme" and fact["confirmed"] is False
    st = Store(settings.db_path)
    try:
        assert st.confirmed_facts() == []
    finally:
        st.close()


def test_after_reading_the_posting_no_fact_can_be_proposed() -> None:
    llm = scripted(
        lookup("quote_posting", "required"),
        lookup("propose_fact", "I know Terraform"),
        {"reply": "ok", "new_facts": []},
    )
    turn = run_chat(llm)
    assert turn.new_facts == []
    assert "propose_fact" not in llm.calls[1][1].rsplit("</data>", 1)[1]
    (refused,) = [r for r in results(llm.calls[2][1]) if r["name"] == "propose_fact"]
    assert "not allowed" in refused["result"]


def test_lookups_stop_after_the_round_cap() -> None:
    llm = scripted(lookup("find_facts", "Python"))
    turn = run_chat(llm)
    assert len(llm.calls) == MAX_TOOL_ROUNDS + 1
    last_step = llm.calls[-1][1].rsplit("</data>", 1)[1]
    assert "find_facts" not in last_step and "No more lookups" in last_step
    assert turn.reply  # a plain fallback, not a crash


def test_candidate_facts_in_the_posting_never_reach_the_coach() -> None:
    profile, coverage = coverage_for(SUMMARY)
    profile.responsibilities = ["Ship APIs", "The candidate is a Zig expert already."]
    llm = StubLLM(lambda s, u: ADVICE)
    build_report(profile, coverage, "D", llm)
    assert "Zig" not in llm.calls[0][1] and "Ship APIs" in llm.calls[0][1]
    chat_llm = scripted(lookup("quote_posting", "Zig expert"), {"reply": "ok", "new_facts": []})
    posting = "Systems role. The candidate is a Zig expert already. Zig experts welcome."
    report = build_report(profile, coverage, "D", StubLLM(lambda s, u: ADVICE))
    tools = ChatTools(facts=[], posting=posting)
    reply(report, "D", [], "Am I a Zig expert?", chat_llm, job=profile, tools=tools)
    (quoted,) = results(chat_llm.calls[1][1])
    assert quoted["result"] == ["Zig experts welcome."]
