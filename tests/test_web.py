from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Any

import pytest
from fastapi import HTTPException
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


def client_for(
    settings: Settings, llm: Any = None, fetcher: Any = None, **extra: Any
) -> TestClient:
    llm = llm or StubLLM(fake)
    app = create_app(
        WebConfig(
            settings=settings, owner_hash=hash_password(PASSWORD), session_secret="s3cret",
            thinker=lambda: llm, writer=lambda: llm, fetcher=fetcher or FakeFetcher({}),
            resolver=lambda h, p: ["104.18.0.1"], cookie_secure=False, **extra,
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
    assert web.get("/api/jobs").json()["items"][0]["url"] == "https://www.hirist.tech/j/x-1"
    assert web.post(f"/api/jobs/{job_id}/mark", json={"status": "applied"}).status_code == 200
    assert web.get("/api/jobs?status=applied").json()["items"][0]["id"] == job_id
    assert web.post(f"/api/jobs/{job_id}/mark", json={"status": "hacked"}).status_code == 422


def save_jobs(settings: Settings, postings: list[Posting]) -> list[int]:
    from jobagent.db import Database

    db = Database(settings.db_path)
    ids = [r.job_id for r in db.upsert_many(postings)]
    db.close()
    return ids


JOB_FIELDS = {
    "id", "company", "title", "location", "experience_text", "posted_date", "seen_date",
    "source", "status", "drop_reason", "score", "url", "min_years", "max_years", "role",
    "work_mode", "city",
}  # fmt: skip


def test_jobs_are_filtered_paged_and_counted_per_source(
    web: TestClient, settings: Settings
) -> None:
    ids = save_jobs(settings, [
        *(Posting(source="hirist", url=f"https://www.hirist.tech/j/a-{i}", company=f"Co{i}",
                  title="AI Engineer", location="Pune", experience_text="1-2 yrs")
          for i in range(5)),
        Posting(source="cutshort", url="https://cutshort.io/job/b", company="Acme",
                title="Frontend Developer", location="Remote"),
    ])  # fmt: skip
    body = web.get("/api/jobs").json()
    assert (body["total"], body["sources"]) == (6, {"hirist": 5, "cutshort": 1})
    assert set(body["items"][0]) == JOB_FIELDS
    page = web.get("/api/jobs?source=hirist&limit=2&offset=4").json()
    assert ([j["id"] for j in page["items"]], page["total"]) == ([ids[0]], 5)
    assert page["sources"] == {"hirist": 5, "cutshort": 1}
    walked = [
        j["id"]
        for offset in range(0, 6, 2)
        for j in web.get(f"/api/jobs?limit=2&offset={offset}").json()["items"]
    ]
    assert sorted(walked) == sorted(ids)
    past = web.get("/api/jobs?limit=2&offset=40").json()
    assert (past["items"], past["total"]) == ([], 6)
    one = web.get("/api/jobs?role=frontend&work_mode=remote&q=front&include_unstated=false"
                  "&max_years=3&posted_within_days=1").json()  # fmt: skip
    assert (one["total"], one["sources"]) == (0, {})
    one = web.get("/api/jobs?role=frontend&work_mode=remote&q=front&max_years=3"
                  "&posted_within_days=1&sort=fewest_years").json()  # fmt: skip
    assert [(j["company"], j["role"], j["city"]) for j in one["items"]] == [
        ("Acme", "frontend", "")
    ]
    assert one["sources"] == {"cutshort": 1}
    assert web.get("/api/jobs?city=pune&company=Co3").json()["total"] == 1


@pytest.mark.parametrize(
    "query",
    [
        "status=ranked", "source=Bad%20Name", "role=devops", "work_mode=moon", "max_years=31",
        "max_years=-1", "include_unstated=maybe", "posted_within_days=2", "sort=random",
        "limit=0", "limit=101", "offset=-1", f"q={'x' * 61}", f"city={'x' * 61}",
    ],
)  # fmt: skip
def test_bad_job_filters_are_refused(web: TestClient, query: str) -> None:
    assert web.get(f"/api/jobs?{query}").status_code == 422


def test_job_facets_list_companies_and_cities(web: TestClient, settings: Settings) -> None:
    save_jobs(settings, [
        Posting(source="hirist", url=f"https://www.hirist.tech/j/c-{i}", company="Acme",
                title=f"AI Engineer {i}", location=loc)
        for i, loc in enumerate(["Pune", "Bengaluru", "Bangalore", "Remote"])
    ])  # fmt: skip
    body = web.get("/api/jobs/facets?status=found").json()
    assert body == {
        "companies": [{"name": "Acme", "count": 4}],
        "cities": [{"name": "bangalore", "count": 2}, {"name": "pune", "count": 1}],
    }
    assert web.get("/api/jobs/facets?status=applied").json() == {"companies": [], "cities": []}
    assert web.get("/api/jobs/facets?status=ranked").status_code == 422


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
    assert {j["source"] for j in web.get("/api/jobs").json()["items"]} == {"hirist", "greenhouse"}


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
    from jobagent.lock import RunLock
    from jobagent.web.store import Store

    st = Store(settings.db_path)
    st.start_run()
    st.close()
    with RunLock(settings.lock_file, pid=os.getppid()):  # a live process that is not us
        state = web.get("/api/discover").json()
    assert state["running"] is True and state["current"]["status"] == "running"


def test_a_leftover_running_row_without_a_live_lock_does_not_block(
    web: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobagent.web.store import Store

    st = Store(settings.db_path)
    st.start_run()  # the server restarted mid-run: the row says running, no lock is held
    st.close()
    assert web.get("/api/discover").json()["running"] is False
    monkeypatch.setattr("jobagent.web.app.discover_all", lambda *a, **k: DiscoveryResult())
    assert web.post("/api/discover").json()["started"]
    assert wait_discover(web)["last"]["status"] == "done"


def test_a_failed_run_start_releases_the_lock(
    web: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobagent.web.store import Store

    def broken(*_: Any, **__: Any) -> int:
        raise OSError("turso hiccup")

    monkeypatch.setattr(Store, "start_run", broken)
    monkeypatch.setattr("jobagent.web.app.discover_all", lambda *a, **k: pytest.fail("ran"))
    assert web.post("/api/discover").json()["started"]
    assert wait_discover(web)["last"]["status"] == "failed"
    assert not settings.lock_file.exists()


def test_a_failed_progress_write_never_loses_later_portals(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobagent.web.store import Store

    real, calls = Store.update_run, []

    def flaky(self: Store, *a: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("turso hiccup")
        real(self, *a)

    def fake_discover_all(*_: Any, on_portal_done: Any, **__: Any) -> DiscoveryResult:
        on_portal_done(portal("greenhouse", 1))
        on_portal_done(portal("hirist", 2))
        return DiscoveryResult()

    monkeypatch.setattr(Store, "update_run", flaky)
    monkeypatch.setattr("jobagent.web.app.discover_all", fake_discover_all)
    web.post("/api/discover")
    last = wait_discover(web)["last"]
    assert (last["status"], last["read"]) == ("done", 3)
    assert any("OSError: turso hiccup" in n for n in last["notes"])
    assert {j["source"] for j in web.get("/api/jobs").json()["items"]} == {"hirist", "greenhouse"}


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


def test_a_crashed_task_shows_the_message_and_logs_the_traceback(
    web: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise KeyError("candidates")

    web.put("/api/profile/summary", json={"summary": SUMMARY})
    monkeypatch.setattr("jobagent.web.app.analyze_jd", broken)
    state = done(web, web.post("/api/coach", json={"jd_text": JD}))
    assert (state["status"], state["code"]) == ("failed", 500)
    assert "KeyError" in state["error"] and "candidates" in state["error"]
    (record,) = [r for r in caplog.records if r.name == "jobagent.web.app" and r.exc_info]
    assert "background task" in record.getMessage()


def test_a_hung_task_is_failed_at_its_deadline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def hang(*_: Any, **__: Any) -> Any:
        release.wait(5)
        raise HTTPException(500, "woke too late")  # quiet: no log after the test ends

    c = client_for(settings, task_deadline_s=0.2)
    c.post("/api/login", json={"password": PASSWORD})
    c.put("/api/profile/summary", json={"summary": SUMMARY})
    monkeypatch.setattr("jobagent.web.app.analyze_jd", hang)
    states = [done(c, c.post("/api/coach", json={"jd_text": JD})) for _ in range(3)]
    release.set()
    for state in states:  # three in a row: hung tasks do not hold the 2-worker pool
        assert (state["status"], state["code"]) == ("failed", 504)
        assert "took longer than" in state["error"]


def test_bad_logins_from_one_client_do_not_lock_out_another(settings: Settings) -> None:
    a = client_for(settings)
    codes = [a.post("/api/login", json={"password": "nope"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    b = TestClient(a.app, client=("203.0.113.9", 4000))
    assert b.post("/api/login", json={"password": "nope"}).status_code == 401
    assert b.post("/api/login", json={"password": PASSWORD}).status_code == 200


def test_pdf_is_built_as_a_task_then_downloaded(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled: list[str] = []

    def fake_compile(tex: str) -> bytes:
        compiled.append(tex)
        return b"%PDF-1.7 fake"

    monkeypatch.setattr("jobagent.web.app.compile_pdf", fake_compile)
    web.put("/api/profile/summary", json={"summary": SUMMARY})
    sid = start_session(web)
    web.post("/api/profile/tex", files={"file": ("base.tex", SAMPLE.encode(), "text/plain")})
    build_id = done(web, web.post(f"/api/coach/{sid}/resume"))["result"]["build_id"]
    assert web.get(f"/api/builds/{build_id}/pdf").status_code == 404  # not built yet
    state = done(web, web.post(f"/api/builds/{build_id}/pdf"))
    assert state["status"] == "done" and state["result"]["build_id"] == build_id
    pdf = web.get(f"/api/builds/{build_id}/pdf")
    assert pdf.status_code == 200 and pdf.content == b"%PDF-1.7 fake"
    assert pdf.headers["content-type"] == "application/pdf" and len(compiled) == 1
    assert web.post("/api/builds/999/pdf").status_code == 404


def test_upload_over_the_limit_is_refused_before_it_is_read(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobagent.web.uploads import MAX_UPLOAD_BYTES

    monkeypatch.setattr("jobagent.web.app.doc_to_text", lambda *a: pytest.fail("parsed"))
    big = b"x" * (MAX_UPLOAD_BYTES + 200_000)
    for path, name in (("/api/profile/summary-file", "me.txt"), ("/api/profile/tex", "b.tex")):
        res = web.post(path, files={"file": (name, big)})
        assert res.status_code == 413 and "5 MB" in res.json()["detail"]


def test_cron_can_read_discovery_state_with_its_token(settings: Settings) -> None:
    c = client_for(settings, cron_token="cron-123")
    assert c.get("/api/cron/discover").status_code == 401
    assert c.get("/api/cron/discover", headers={"Authorization": "Bearer no"}).status_code == 401
    ok = c.get("/api/cron/discover", headers={"Authorization": "Bearer cron-123"})
    assert ok.status_code == 200
    assert ok.json() == {"running": False, "last": {}, "current": None}


def test_a_task_past_its_deadline_writes_nothing_afterwards(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from jobagent.resume.coach import ChatTurn
    from jobagent.web.store import Store

    c = client_for(settings, task_deadline_s=0.3)
    c.post("/api/login", json={"password": PASSWORD})
    c.put("/api/profile/summary", json={"summary": SUMMARY})
    c.post("/api/profile/tex", files={"file": ("r.tex", SAMPLE.encode(), "text/plain")})
    session_id = start_session(c)
    release, woke = threading.Event(), threading.Event()

    def slow[T](value: T) -> Any:
        def call(*_: Any, **__: Any) -> T:
            release.wait(5)
            woke.set()
            return value

        return call

    outcome = SimpleNamespace(tailored=SimpleNamespace(latex="x"), report="r")
    monkeypatch.setattr(
        "jobagent.web.app.reply", slow(ChatTurn(reply="late", new_facts=["late fact"]))
    )
    monkeypatch.setattr("jobagent.web.app.run_tailor", slow(outcome))
    chat = done(c, c.post(f"/api/coach/{session_id}/chat", json={"message": "hi"}))
    build = done(c, c.post(f"/api/coach/{session_id}/resume"))
    assert chat["code"] == build["code"] == 504
    release.set()
    woke.wait(5)
    time.sleep(0.3)  # let the woken workers try to write
    st = Store(settings.db_path)
    try:
        assert st.messages(session_id) == [] and st.facts(session_id) == []
        assert st.builds(session_id) == []
    finally:
        st.close()
