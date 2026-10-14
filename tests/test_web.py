from __future__ import annotations

import shutil
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.discovery.base import DiscoveryResult, FetchResult, Unreachable
from jobagent.llm import FailingLLM, StubLLM
from jobagent.models import Posting
from jobagent.web.app import WebConfig, compile_pdf, create_app, keep_awake
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


def test_cron_trigger_needs_its_token(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []

    class FakeThread:
        def __init__(self, **kw: Any) -> None:
            pass

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr("jobagent.web.app.threading.Thread", FakeThread)
    llm = StubLLM(fake)
    app = create_app(
        WebConfig(settings, hash_password(PASSWORD), "s3cret", lambda: llm, lambda: llm,
                  cron_token="cron-123")
    )  # fmt: skip
    c = TestClient(app)
    assert c.post("/api/cron/discover").status_code == 401
    assert c.post("/api/cron/discover", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = c.post("/api/cron/discover", headers={"Authorization": "Bearer cron-123"})
    assert ok.json()["started"] and ok.json()["running"] and started == [True]
    assert ok.json()["last"] == {}


def wait_discover(c: TestClient) -> dict[str, Any]:
    for _ in range(200):
        state: dict[str, Any] = c.get("/api/discover").json()
        if not state["running"]:
            return state
        time.sleep(0.02)
    raise AssertionError("discovery did not finish")


def portal(source: str, n: int) -> DiscoveryResult:
    return DiscoveryResult(postings=[
        Posting(source=source, url=f"https://{source}.example/{i}", company=f"{source}{i}",
                title="AI Engineer", location="Bengaluru") for i in range(n)
    ])  # fmt: skip


@pytest.mark.parametrize("crash", [False, True])
def test_discover_saves_each_portal_and_keeps_them_after_a_crash(
    web: TestClient, monkeypatch: pytest.MonkeyPatch, crash: bool
) -> None:
    def fake_discover_all(*_: Any, on_portal_done: Any, **__: Any) -> DiscoveryResult:
        merged = DiscoveryResult()
        for part in (portal("hirist", 2), portal("greenhouse", 1)):
            on_portal_done(part)
            merged.extend(part)
        if crash:
            raise RuntimeError("portal 3 died")
        return merged

    monkeypatch.setattr("jobagent.web.app.discover_all", fake_discover_all)
    assert web.post("/api/discover").json()["started"]
    last = wait_discover(web)["last"]
    if crash:
        assert "portal 3 died" in last["error"] and last["new"] == 3
    else:
        assert (last["read"], last["new"]) == (3, 3)
    assert {j["source"] for j in web.get("/api/jobs").json()} == {"hirist", "greenhouse"}


def logged_in(settings: Settings) -> TestClient:
    c = client_for(settings)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return c


def test_last_run_is_kept_in_the_db_and_shown_after_a_restart(
    web: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    senior = Posting(
        source="hirist", url="https://hirist.example/s", company="Zed",
        title="AI Engineer", location="Bengaluru", experience_text="12+ years",
    )  # fmt: skip

    def fake_discover_all(*_: Any, on_portal_done: Any, **__: Any) -> DiscoveryResult:
        part = portal("hirist", 2)
        part.postings.append(senior)
        part.portal, part.notes = "hirist", ["hirist: ai: truncated at 1 pages"]
        on_portal_done(part)
        gh = portal("greenhouse", 1)
        gh.unreachable = [Unreachable("Acme", "u", "http 403")]
        on_portal_done(gh)
        return DiscoveryResult()

    monkeypatch.setattr("jobagent.web.app.discover_all", fake_discover_all)
    assert web.post("/api/discover").json()["started"]
    wait_discover(web)
    state = logged_in(settings).get("/api/discover").json()  # a fresh app, as after a restart
    last = state["last"]
    assert state["running"] is False and state["current"] is None
    assert (last["status"], last["error"], last["read"], last["new"]) == ("done", "", 4, 3)
    assert last["portals"]["hirist"] == {"read": 3, "new": 2, "saved": 3}
    assert last["drop_reasons"] == {"too_senior": 1}
    assert last["unreachable"] == ["Acme: http 403"]
    assert last["notes"] == ["hirist: ai: truncated at 1 pages"]
    assert last["started_at"] and last["finished_at"]


def test_a_lock_that_raises_an_unexpected_error_still_ends_the_run(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Any) -> None:
        raise OSError("data dir is read-only")

    monkeypatch.setattr("jobagent.web.app.RunLock.acquire", broken)
    monkeypatch.setattr("jobagent.web.app.discover_all", lambda *a, **k: pytest.fail("ran"))
    assert web.post("/api/discover").json()["started"]
    state = wait_discover(web)
    assert state["last"]["status"] == "failed"
    assert "OSError: data dir is read-only" in state["last"]["error"]
    assert web.post("/api/discover").json()["started"]  # not stuck


def test_a_run_left_running_too_long_reads_as_failed(web: TestClient, settings: Settings) -> None:
    from jobagent.web.store import Store

    st = Store(settings.db_path)
    st.start_run(started_at=time.time() - 31 * 60)
    st.close()
    state = web.get("/api/discover").json()
    assert state["running"] is False and state["current"] is None
    assert state["last"]["status"] == "failed" and "did not finish" in state["last"]["error"]


def test_a_recent_run_from_another_process_shows_as_running(
    web: TestClient, settings: Settings
) -> None:
    from jobagent.web.store import Store

    st = Store(settings.db_path)
    st.start_run()
    st.close()
    state = web.get("/api/discover").json()
    assert state["running"] is True and state["current"]["status"] == "running"


def test_kill_switch_survives_a_restart_and_reaches_the_run(
    web: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def fake_discover_all(_c: Any, _f: Any, policy: Any, *_: Any, **__: Any) -> DiscoveryResult:
        seen.append(policy.stopped())
        return DiscoveryResult()

    assert web.post("/api/kill-switch/on").json() == {"kill_switch": True}
    assert not settings.stop_file.exists()
    again = logged_in(settings)  # a fresh app with the same DB
    assert again.get("/api/me").json()["kill_switch"] is True
    monkeypatch.setattr("jobagent.web.app.discover_all", fake_discover_all)
    again.post("/api/discover")
    wait_discover(again)
    assert seen == [True]
    again.post("/api/kill-switch/off")
    assert web.get("/api/me").json()["kill_switch"] is False


def test_self_ping_uses_the_public_url_while_running(monkeypatch: pytest.MonkeyPatch) -> None:
    pinged: list[str] = []
    got_two = threading.Event()

    def ping(url: str) -> None:
        pinged.append(url)
        if len(pinged) >= 2:
            got_two.set()

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://job-jarvis.onrender.com/")
    stop = threading.Event()
    thread = keep_awake(stop, ping, every_s=0.01)
    assert thread is not None and got_two.wait(2)
    stop.set()
    thread.join(2)
    assert set(pinged) == {"https://job-jarvis.onrender.com/api/health"}


def test_self_ping_is_skipped_without_a_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert keep_awake(threading.Event(), lambda url: pytest.fail("pinged"), every_s=0.01) is None


def test_schema_runs_once_and_connections_are_reused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobagent import sqlconn

    opened: list[str] = []
    scripts: list[str] = []

    def counting_connect(path: Any) -> Any:
        conn = sqlconn.connect(path)
        opened.append(str(path))

        class Spy:
            def __getattr__(self, name: str) -> Any:
                return getattr(conn, name)

            def executescript(self, script: str) -> Any:
                scripts.append(script)
                return conn.executescript(script)

            def __enter__(self) -> Any:
                return conn.__enter__()

            def __exit__(self, *exc: Any) -> Any:
                return conn.__exit__(*exc)

        return Spy()

    monkeypatch.setattr("jobagent.db.connect", counting_connect)
    monkeypatch.setattr("jobagent.web.store.connect", counting_connect)
    c = client_for(settings)
    c.post("/api/login", json={"password": PASSWORD})
    for _ in range(10):
        assert c.get("/api/jobs").status_code == 200
        assert c.get("/api/profile").status_code == 200
    assert len(scripts) == 2  # one schema each for jobs and the web store
    assert len(opened) == 2


def test_cron_trigger_is_off_without_a_token(settings: Settings) -> None:
    c = client_for(settings)
    assert c.post("/api/cron/discover", headers={"Authorization": "Bearer "}).status_code == 401


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
def test_pdftex_template_compiles_under_tectonic() -> None:
    pdf = compile_pdf(SAMPLE)
    assert pdf.startswith(b"%PDF")
