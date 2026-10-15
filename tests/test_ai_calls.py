"""U6: fewer, smaller AI calls. Call counts follow the target table in the tokens audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobagent.config import Settings
from jobagent.llm import StubLLM
from jobagent.resume import cli_tailor
from jobagent.resume.coach import (
    MAX_HISTORY_CHARS,
    MAX_REPLY_CHARS,
    SYSTEM,
    Message,
    build_report,
    profile_digest,
    recent_history,
    reply,
    shared_prefix,
)
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.latex import parse
from jobagent.resume.match import match_profile
from jobagent.resume.rewrite import tailor_resume
from jobagent.web.store import Store
from tests.test_jd import GOOD, JD, router
from tests.test_rewrite import CARDS, PROFILE, SAMPLE, bullets_in
from tests.test_web import ADVICE, PASSWORD, SUMMARY, client_for, done, fake
from tests.test_web import JD as WEB_JD

LONG_SUMMARY = "\n".join(
    f"Role {i}: built Python APIs for team {i} over 2 years." for i in range(120)
)


def test_jd_analysis_is_one_call_at_most_two() -> None:
    llm = router()
    profile = analyze_jd(JD, llm)
    assert len(llm.calls) == 1 and profile.rounds == 1
    assert set(profile.must_have) == {"Python", "FastAPI", "LangGraph", "RAG"}


def test_extraction_prompt_lists_terms_the_code_scan_found() -> None:
    llm = router()
    analyze_jd(JD, llm)
    ((_, user),) = llm.calls
    assert "Known terms seen in the text" in user and "Langfuse" in user


def test_a_reply_with_no_grounded_skill_gets_one_repair_call() -> None:
    replies = [{**GOOD, "skills": [{"name": "Haskell", "importance": "must"}]}, GOOD]
    llm = StubLLM(lambda s, u: replies.pop(0))
    profile = analyze_jd(JD, llm)
    assert len(llm.calls) == 2 and llm.steps == ["jd_extract", "jd_repair"]
    assert "Python" in profile.must_have and profile.rounds == 2


def test_a_still_broken_repair_stops_after_two_calls() -> None:
    llm = StubLLM(lambda s, u: "not json")
    profile = analyze_jd(JD, llm)
    assert len(llm.calls) == 2 and profile.degraded


def bold_first_word(text: str) -> str:
    head, _, rest = text.partition(" ")
    return f"**{head}** {rest}"


def test_all_bullets_are_rewritten_in_one_call_and_checked_one_by_one() -> None:
    def respond(system: str, user: str) -> Any:
        sent = bullets_in(user)
        bad = min(sent, key=lambda b: len(b[1]))[0]
        return {
            "items": [
                {"id": i, "text": text + " by 99%" if i == bad else bold_first_word(text)}
                for i, text in sent
            ]
        }

    llm = StubLLM(respond)
    result = tailor_resume(parse(SAMPLE), CARDS, PROFILE, match_profile(PROFILE, CARDS), llm)
    assert len(llm.calls) == 1 and llm.steps == ["rewrite_bullets"]
    sent = bullets_in(llm.calls[0][1])
    assert len(sent) >= 10
    reasons = [r for _, r in result.rejected]
    assert reasons.count("new number") == 1  # the bad item is refused alone
    # The existing length rule still refuses edits to bullets already over its limit.
    assert set(reasons) <= {"new number", "too long"}
    bullets = [c for c in result.changes if c.kind == "bullet"]
    assert bullets and len(bullets) + len(reasons) == len(sent)


def test_a_bullet_the_model_left_out_stays_as_it_was_and_is_reported() -> None:
    llm = StubLLM(lambda s, u: {"items": [{"id": 1, "text": bold_first_word(bullets_in(u)[1][1])}]})
    result = tailor_resume(parse(SAMPLE), CARDS, PROFILE, match_profile(PROFILE, CARDS), llm)
    assert len([c for c in result.changes if c.kind == "bullet"]) == 1
    assert result.failed and not result.rejected
    assert {cause for _, cause in result.failed} == {"model reply left this bullet out"}


def test_cli_thinks_at_temperature_0_and_writes_at_0_5() -> None:
    thinker, writer = cli_tailor.build_llms()
    assert (thinker.temperature, writer.temperature) == (0.0, 0.5)  # type: ignore[attr-defined]


def test_short_summary_is_sent_as_it_is() -> None:
    llm = StubLLM(lambda s, u: {"digest": "x"})
    assert profile_digest(SUMMARY, llm) == SUMMARY and llm.calls == []


def test_long_summary_is_condensed_once_and_stored(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.sqlite3")
    llm = StubLLM(lambda s, u: {"digest": "Python APIs, 2 years per role."})
    assert profile_digest(LONG_SUMMARY, llm, store) == "Python APIs, 2 years per role."
    assert profile_digest(LONG_SUMMARY, llm, store) == "Python APIs, 2 years per role."
    assert len(llm.calls) == 1 and llm.steps == ["profile_digest"]
    profile_digest(LONG_SUMMARY + "\nNew role.", llm, store)
    assert len(llm.calls) == 2  # summary changed, digest refreshed


def test_report_and_chat_start_with_the_same_job_and_profile() -> None:
    profile = analyze_jd(JD, router())
    coverage = match_profile(profile, cards_from_summary(SUMMARY))
    llm = StubLLM(lambda s, u: ADVICE if "Write the fit report" in u else {"reply": "ok"})
    report = build_report(profile, coverage, "DIGEST", llm)
    reply(report, "DIGEST", [Message("user", "hi")], "What next?", llm, job=profile)
    prefix = shared_prefix(profile, "DIGEST")
    (s1, u1), (s2, u2) = llm.calls
    assert s1 == s2 and s1.endswith(SYSTEM)
    assert u1.startswith(prefix) and u2.startswith(prefix)
    assert u1.rstrip().endswith("at most 3]}.")  # step instructions come last
    assert "latest message" in u2.split('<data name="LATEST">')[1]
    assert llm.steps == ["coach_report", "coach_chat"]


def test_chat_history_is_capped_by_characters_and_keeps_the_newest() -> None:
    history = [Message("user" if i % 2 else "coach", f"m{i} " + "x" * 3_990) for i in range(40)]
    talk = recent_history(history)
    assert len(talk) <= MAX_HISTORY_CHARS
    assert talk.splitlines()[-1].startswith("user: m39")
    assert "m30 " not in talk
    llm = StubLLM(lambda s, u: {"reply": "y" * 5_000})
    profile = analyze_jd(JD, router())
    report = build_report(profile, match_profile(profile, []), "D", StubLLM(lambda s, u: ADVICE))
    turn = reply(report, "D", history, "next?", llm, job=profile)
    chat = llm.calls[0][1].split('<data name="CHAT">\n')[1].split("\n</data>")[0]
    assert len(chat) <= MAX_HISTORY_CHARS
    assert len(turn.reply) == MAX_REPLY_CHARS


def test_one_huge_message_is_cut_not_dropped() -> None:
    talk = recent_history([Message("user", "z" * 10_000)])
    assert talk.startswith("user: z") and len(talk) == MAX_HISTORY_CHARS


def journey(settings: Settings, summary: str) -> StubLLM:
    def respond(system: str, user: str) -> Any:
        if "Condense one person's career summary" in system:
            return {"digest": "Python APIs and Langfuse tracing."}
        return fake(system, user)

    llm = StubLLM(respond)
    c = client_for(settings, llm=llm)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    c.put("/api/profile/summary", json={"summary": summary})
    c.post("/api/profile/tex", files={"file": ("r.tex", SAMPLE.encode(), "text/plain")})
    sid = done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))["result"]["session_id"]
    for i in range(5):
        state = done(c, c.post(f"/api/coach/{sid}/chat", json={"message": f"question {i}"}))
        assert state["status"] == "done", state
    assert done(c, c.post(f"/api/coach/{sid}/resume"))["status"] == "done"
    return llm


def test_web_journey_uses_eight_calls(settings: Settings) -> None:
    llm = journey(settings, SUMMARY)
    assert llm.steps == ["jd_extract", "coach_report", *["coach_chat"] * 5, "rewrite_bullets"]


def test_long_summary_journey_condenses_once_and_never_sends_it_raw(settings: Settings) -> None:
    llm = journey(settings, LONG_SUMMARY)
    assert llm.steps.count("profile_digest") == 1 and len(llm.calls) == 9
    coach_users = [u for (_, u), step in zip(llm.calls, llm.steps, strict=True) if "coach" in step]
    assert all("Role 119" not in u and "Langfuse tracing" in u for u in coach_users)


@pytest.mark.parametrize("step", ["jd_extract", "coach_report", "coach_chat", "rewrite_bullets"])
def test_every_flow_step_has_an_output_cap(step: str) -> None:
    assert Settings().llm.max_output_tokens[step] > 0
