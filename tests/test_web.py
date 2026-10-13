from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.discovery.base import FetchResult
from jobagent.llm import FailingLLM, StubLLM
from jobagent.web.app import WebConfig, create_app
from jobagent.web.auth import hash_password, make_token, token_valid
from tests.test_discovery import FIX, FakeFetcher
from tests.test_tailor_cli import EXTRACT, SAMPLE

JD = "Senior AI Engineer\n5+ years. Python and Terraform are required. Langfuse is nice to have.\n"
SUMMARY = "I build APIs in Python.\nI trace LLM apps with Langfuse."
PASSWORD = "correct horse battery"
ADVICE = {
    "fit": 3, "summary": "Close fit.", "good": ["Python APIs"],
    "learn": [{"topic": "Terraform", "why": "must-have", "resource": "HashiCorp docs", "days": 7}],
    "risks": ["No Terraform in production"],
}  # fmt: skip


def fake(system: str, user: str) -> Any:
    if "check a skill list" in system:
        return {"missing": [], "unsupported": []}
    if "Rewrite one resume bullet" in system:
        return {"text": "unchanged"}
    if "latest message" in system:
        return {"reply": "Terraform is the main gap.", "new_facts": ["I used Terraform at Acme"]}
    if "career coach" in system:
        return ADVICE
    return EXTRACT


def client_for(settings: Settings, llm: Any = None, fetcher: Any = None) -> TestClient:
    llm = llm or StubLLM(fake)
    app = create_app(
        WebConfig(
            settings=settings, owner_hash=hash_password(PASSWORD), session_secret="s3cret",
            thinker=lambda: llm, writer=lambda: llm, fetcher=fetcher or FakeFetcher({}),
            resolver=lambda h, p: ["104.18.0.1"], cookie_secure=False,
        )
    )  # fmt: skip
    return TestClient(app)


def done(c: TestClient, res: Any) -> dict[str, Any]:
    """Wait for a background task started by `res` and return its final state."""
    assert res.status_code == 200, res.text
    task_id = res.json()["task_id"]
    for _ in range(200):
        state: dict[str, Any] = c.get(f"/api/tasks/{task_id}").json()
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def start_session(c: TestClient, **body: Any) -> int:
    state = done(c, c.post("/api/coach", json={"jd_text": JD, **body}))
    assert state["status"] == "done", state
    return int(state["result"]["session_id"])


@pytest.fixture
def web(settings: Settings) -> TestClient:
    c = client_for(settings)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return c


def test_every_route_but_login_and_health_needs_a_session(settings: Settings) -> None:
    c = client_for(settings)
    assert c.get("/api/health").status_code == 200
    for path in (
        "/api/me",
        "/api/jobs",
        "/api/profile",
        "/api/coach",
        "/api/builds",
        "/api/tasks/x",
    ):
        assert c.get(path).status_code == 401
    assert c.post("/api/coach", json={"jd_text": JD}).status_code == 401
    c.cookies.set("jobagent_session", "forged")
    assert c.get("/api/me").status_code == 401


def test_wrong_password_is_refused_and_repeated_tries_are_limited(settings: Settings) -> None:
    c = client_for(settings)
    codes = [c.post("/api/login", json={"password": "nope"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_tokens_expire_and_cannot_be_signed_with_another_secret() -> None:
    token = make_token("a", now=1000)
    assert token_valid("a", token, now=1001)
    assert not token_valid("b", token, now=1001)
    assert not token_valid("a", token, now=1000 + 8 * 24 * 3600)


def test_app_refuses_to_start_without_secrets(settings: Settings) -> None:
    with pytest.raises(RuntimeError):
        create_app(
            WebConfig(settings, "", "", thinker=lambda: StubLLM(fake), writer=lambda: StubLLM(fake))
        )


def test_coach_needs_a_profile_then_reports_fit_and_gaps(web: TestClient) -> None:
    assert web.post("/api/coach", json={"jd_text": JD}).status_code == 400
    web.put("/api/profile/summary", json={"summary": SUMMARY})
    sid = start_session(web, company="Acme")
    report = web.get(f"/api/coach/{sid}").json()["report"]
    assert report["fit"] == 3
    assert "Python" in report["proven"]
    assert report["missing_must"] == ["Terraform"]
    assert report["learn"][0]["topic"] == "Terraform"


def test_chat_saves_history_and_facts_wait_for_confirmation(web: TestClient) -> None:
    web.put("/api/profile/summary", json={"summary": SUMMARY})
    sid = start_session(web)
    done(web, web.post(f"/api/coach/{sid}/chat", json={"message": "What am I missing?"}))
    view = web.get(f"/api/coach/{sid}").json()
    assert [m["role"] for m in view["messages"]] == ["user", "coach"]
    (fact,) = view["facts"]
    assert fact == {"id": fact["id"], "text": "I used Terraform at Acme", "confirmed": False}
    assert (
        web.post(f"/api/coach/{sid}/facts/{fact['id']}", json={"confirmed": True}).status_code
        == 200
    )
    assert web.get(f"/api/coach/{sid}").json()["facts"][0]["confirmed"] is True
    assert web.post(f"/api/coach/{sid}/facts/999", json={"confirmed": True}).status_code == 404


def test_resume_build_uses_confirmed_facts_and_returns_tex(web: TestClient) -> None:
    web.put("/api/profile/summary", json={"summary": SUMMARY})
    sid = start_session(web)
    assert web.post(f"/api/coach/{sid}/resume").status_code == 400  # no base .tex yet
    up = web.post("/api/profile/tex", files={"file": ("base.tex", SAMPLE.encode(), "text/plain")})
    assert up.status_code == 200
    built = done(web, web.post(f"/api/coach/{sid}/resume"))
    assert built["status"] == "done", built
    tex = web.get(f"/api/builds/{built['result']['build_id']}/tex")
    assert tex.status_code == 200 and "\\begin{document}" in tex.text
    assert web.get("/api/builds").json()[0]["session_id"] == sid


def test_ai_failure_is_a_clear_error_not_a_crash(settings: Settings) -> None:
    c = client_for(settings, llm=FailingLLM())
    c.post("/api/login", json={"password": PASSWORD})
    c.put("/api/profile/summary", json={"summary": SUMMARY})
    # The JD reader falls back to a plain scan; the advice step must fail cleanly.
    state = done(c, c.post("/api/coach", json={"jd_text": JD}))
    assert (state["status"], state["code"]) == ("failed", 502)
    assert "AI step failed" in state["error"]
    assert c.get("/api/coach").json() == []


def test_link_on_unknown_site_asks_before_fetching(web: TestClient) -> None:
    web.put("/api/profile/summary", json={"summary": SUMMARY})
    res = web.post("/api/coach", json={"url": "https://careers.example.org/job/1"})
    assert res.status_code == 409


def test_trusted_link_is_read_and_coached(settings: Settings) -> None:
    url = "https://careers.example.org/job/1"
    page = FetchResult(200, (FIX / "pages/manual_posting.html").read_text(), url)
    c = client_for(settings, fetcher=FakeFetcher({url: page}))
    c.post("/api/login", json={"password": PASSWORD})
    c.put("/api/profile/summary", json={"summary": SUMMARY})
    state = done(c, c.post("/api/coach", json={"url": url, "trust_host": True}))
    assert c.get(f"/api/coach/{state['result']['session_id']}").json()["company"] == "Epsilon Labs"


def test_summary_upload_reads_text_and_rejects_other_types(web: TestClient) -> None:
    ok = web.post("/api/profile/summary-file", files={"file": ("me.md", SUMMARY.encode())})
    assert ok.status_code == 200 and web.get("/api/profile").json()["summary"] == SUMMARY
    bad = web.post("/api/profile/summary-file", files={"file": ("me.exe", b"MZ")})
    assert bad.status_code == 400


def test_owner_can_mark_a_job(web: TestClient, settings: Settings) -> None:
    from jobagent.db import Database
    from jobagent.models import Posting

    db = Database(settings.db_path)
    job_id = db.upsert_posting(
        Posting(source="hirist", url="https://www.hirist.tech/j/x-1", company="Acme",
                title="Full Stack Developer", location="Bengaluru")
    ).job_id  # fmt: skip
    db.close()
    assert web.get("/api/jobs").json()[0]["url"] == "https://www.hirist.tech/j/x-1"
    assert web.post(f"/api/jobs/{job_id}/mark", json={"status": "applied"}).status_code == 200
    assert web.get("/api/jobs?status=applied").json()[0]["id"] == job_id
    assert web.post(f"/api/jobs/{job_id}/mark", json={"status": "hacked"}).status_code == 422
