"""Production hardening: login keying, startup checks, health, logs, shutdown, PDF env."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobagent import __version__
from jobagent.config import ConfigError, Settings, check_config
from jobagent.redact import Redactor, configure_logging
from jobagent.web import app as web_app
from jobagent.web.store import Store
from tests.test_web import JD, PASSWORD, SUMMARY, client_for, done

PROD_ENV = {
    "JOBAGENT_OWNER_HASH": "scrypt$00$00",
    "JOBAGENT_SESSION_SECRET": "s",
    "GEMINI_API_KEY": "g",
    "TURSO_DATABASE_URL": "libsql://jobs-owner.turso.io",
    "TURSO_AUTH_TOKEN": "t",
}


def bad_login(c: TestClient, **headers: str) -> int:
    return c.post("/api/login", json={"password": "nope"}, headers=headers).status_code


# --- login keying (KTD8) ---


def test_spoofed_left_most_forwarded_for_does_not_reset_the_counter(settings: Settings) -> None:
    c = client_for(settings)
    codes = [
        bad_login(c, **{"X-Forwarded-For": f"10.9.9.{i}, 198.51.100.7, 76.76.21.1"})
        for i in range(6)
    ]
    assert codes == [401] * 5 + [429]
    # Another client behind the same Vercel hop is not blocked.
    assert bad_login(c, **{"X-Forwarded-For": "198.51.100.8, 76.76.21.1"}) == 401


def test_vercel_client_header_is_the_preferred_key(settings: Settings) -> None:
    c = client_for(settings)
    hop = {"X-Forwarded-For": "1.1.1.1, 76.76.21.1"}
    codes = [bad_login(c, **{"x-vercel-forwarded-for": "203.0.113.5", **hop}) for _ in range(6)]
    assert codes == [401] * 5 + [429]
    assert bad_login(c, **{"x-vercel-forwarded-for": "203.0.113.6", **hop}) == 401


def test_a_global_ceiling_slows_rotating_keys_but_never_refuses_the_owner(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_app, "GLOBAL_DELAY_S", 0.2)
    c = client_for(settings)
    codes = [bad_login(c, **{"x-vercel-forwarded-for": f"203.0.113.{i}"}) for i in range(30)]
    assert set(codes) == {401}
    started = time.monotonic()
    assert bad_login(c, **{"x-vercel-forwarded-for": "198.51.100.200"}) == 401
    assert time.monotonic() - started >= 0.2  # past the ceiling every check is slowed
    owner = c.post(
        "/api/login", json={"password": PASSWORD}, headers={"x-vercel-forwarded-for": "1.2.3.4"}
    )
    assert owner.status_code == 200 and owner.cookies.get("jobagent_session") is not None


def test_slowed_checks_run_one_at_a_time(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_app, "GLOBAL_DELAY_S", 0.0)
    monkeypatch.setattr(web_app, "MAX_GLOBAL_FAILURES", 0)
    live, peak, lock = [0], [0], threading.Lock()

    def slow_verify(password: str, stored: str) -> bool:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return False

    monkeypatch.setattr("jobagent.web.app.verify_password", slow_verify)
    c = client_for(settings)
    threads = [
        threading.Thread(target=bad_login, args=(c,), kwargs={"x-vercel-forwarded-for": f"9.{i}"})
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == 1


def test_forwarded_headers_need_the_proxy_secret_when_one_is_set(settings: Settings) -> None:
    c = client_for(settings, proxy_secret="proxy-s3cret")
    # Spoofed: no secret, so every request keys on the socket address and shares a limit.
    codes = [bad_login(c, **{"x-vercel-forwarded-for": f"203.0.113.{i}"}) for i in range(6)]
    assert codes == [401] * 5 + [429]
    spoofed = {"x-vercel-forwarded-for": "203.0.113.99", "x-jobagent-proxy": "wrong"}
    assert bad_login(c, **spoofed) == 429
    # Through the proxy: the header picks the key.
    via = {"x-jobagent-proxy": "proxy-s3cret"}
    assert bad_login(c, **{"x-vercel-forwarded-for": "198.51.100.1", **via}) == 401


def test_a_missing_proxy_secret_is_logged_once(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="jobagent.web.app")
    c = client_for(settings)
    for _ in range(2):
        bad_login(c, **{"x-vercel-forwarded-for": "203.0.113.57"})
    lines = [r.getMessage() for r in caplog.records if "JOBAGENT_PROXY_SECRET" in r.getMessage()]
    assert len(lines) == 1


def test_the_resolved_key_is_logged_once_with_the_last_octet_masked(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="jobagent.web.app")
    c = client_for(settings)
    for _ in range(2):
        bad_login(c, **{"x-vercel-forwarded-for": "203.0.113.57"})
    lines = [r.getMessage() for r in caplog.records if "login key" in r.getMessage()]
    assert len(lines) == 1
    assert "203.0.113.x" in lines[0] and "203.0.113.57" not in lines[0]


def test_at_most_two_password_checks_run_at_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, peak, lock = [0], [0], threading.Lock()

    def slow_verify(password: str, stored: str) -> bool:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return False

    monkeypatch.setattr("jobagent.web.app.verify_password", slow_verify)
    c = client_for(settings)
    codes: list[int] = []

    def attempt(i: int) -> None:
        codes.append(bad_login(c, **{"x-vercel-forwarded-for": f"203.0.113.{i}"}))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] <= 2 and len(codes) == 10
    assert set(codes) <= {401, 429}


def test_a_login_that_cannot_get_a_slot_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    monkeypatch.setattr(web_app, "LOGIN_WAIT_S", 0.05)
    monkeypatch.setattr("jobagent.web.app.verify_password", lambda p, s: release.wait(5) and False)
    c = client_for(settings)
    busy = [
        threading.Thread(
            target=bad_login, args=(c,), kwargs={"x-vercel-forwarded-for": f"9.9.9.{i}"}
        )
        for i in range(2)
    ]
    for t in busy:
        t.start()
    time.sleep(0.2)
    res = c.post(
        "/api/login", json={"password": "x"}, headers={"x-vercel-forwarded-for": "8.8.8.8"}
    )
    release.set()
    for t in busy:
        t.join(5)
    assert res.status_code == 429 and "busy" in res.json()["detail"]


# --- startup checks (KTD9) ---


def test_check_config_lists_every_missing_name() -> None:
    with pytest.raises(ConfigError) as err:
        check_config({})
    assert "JOBAGENT_OWNER_HASH" in str(err.value)
    assert "JOBAGENT_SESSION_SECRET" in str(err.value)


def test_local_runs_need_only_the_login_secrets() -> None:
    check_config({"JOBAGENT_OWNER_HASH": "h", "JOBAGENT_SESSION_SECRET": "s"})


@pytest.mark.parametrize("marker", [{"RENDER": "true"}, {"JOBAGENT_ENV": "production"}])
def test_production_needs_a_remote_database_and_the_ai_key(marker: dict[str, str]) -> None:
    check_config({**PROD_ENV, **marker})
    env = {"JOBAGENT_OWNER_HASH": "h", "JOBAGENT_SESSION_SECRET": "s", **marker}
    with pytest.raises(ConfigError) as err:
        check_config(env)
    for name in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "GEMINI_API_KEY"):
        assert name in str(err.value)
    with pytest.raises(ConfigError, match="remote"):
        check_config({**PROD_ENV, **marker, "TURSO_DATABASE_URL": "file:/tmp/x.db"})


def test_app_from_env_refuses_to_start_and_names_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (*PROD_ENV, "JOBAGENT_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBAGENT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("JOBAGENT_OWNER_HASH", "h")
    monkeypatch.setenv("JOBAGENT_SESSION_SECRET", "s")
    with pytest.raises(ConfigError, match="TURSO_DATABASE_URL"):
        web_app.app_from_env()


# --- health ---


def test_health_checks_the_database_and_reports_the_version(settings: Settings) -> None:
    c = client_for(settings)
    res = c.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "db": "ok", "version": __version__}
    assert c.get("/api/health/live").json() == {"ok": True}


def test_health_is_503_when_the_database_is_down(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = client_for(settings)

    def broken(*_: Any) -> Any:
        raise RuntimeError("Turso said no")

    monkeypatch.setattr("jobagent.web.app.db_ping", broken)
    res = c.get("/api/health")
    assert res.status_code == 503
    assert res.json()["ok"] is False and res.json()["db"] == "down"
    assert c.get("/api/health/live").status_code == 200


def test_a_hung_database_check_is_reused_not_stacked(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = client_for(settings)
    release, pings = threading.Event(), []

    def hung(*_: Any) -> None:
        pings.append(1)
        release.wait(5)

    monkeypatch.setattr(web_app, "HEALTH_TIMEOUT_S", 0.05)
    monkeypatch.setattr("jobagent.web.app.db_ping", hung)
    assert [c.get("/api/health").status_code for _ in range(3)] == [503] * 3
    assert len(pings) == 1
    release.set()
    for _ in range(100):
        if c.get("/api/health").status_code == 200:
            break
        time.sleep(0.02)
    assert c.get("/api/health").status_code == 200


def test_render_checks_liveness_not_the_database() -> None:
    import yaml

    blueprint = yaml.safe_load((Path(__file__).parents[1] / "render.yaml").read_text())
    assert blueprint["services"][0]["healthCheckPath"] == "/api/health/live"


# --- logs ---


def test_request_id_is_echoed_or_made(settings: Settings) -> None:
    c = client_for(settings)
    assert (
        c.get("/api/health/live", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    made = c.get("/api/health/live", headers={"X-Request-ID": "bad id\n!"}).headers["x-request-id"]
    assert made and made != "bad id\n!"


def test_json_logs_carry_the_request_id_and_stay_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jobagent.redact import request_id

    configure_logging(Redactor(["supersecretvalue99"]), json_lines=True)
    try:
        token = request_id.set("req-7")
        try:
            raise ValueError("boom supersecretvalue99")
        except ValueError:
            logging.getLogger("jobagent.test").exception("call https://x.io/a?key=zz failed")
        request_id.reset(token)
        logging.getLogger("uvicorn.error").warning("uvicorn says supersecretvalue99")
        lines = [json.loads(x) for x in capsys.readouterr().err.splitlines() if x.strip()]
    finally:
        configure_logging(Redactor())
    first, second = lines
    assert first["request_id"] == "req-7" and first["level"] == "ERROR"
    assert "supersecretvalue99" not in json.dumps(lines) and "key=zz" not in json.dumps(lines)
    assert "ValueError" in first["exc"]
    assert second["logger"] == "uvicorn.error" and "[REDACTED]" in second["msg"]


def test_a_task_logs_its_ai_usage_with_the_request_id(
    web: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    from jobagent.redact import RequestIdFilter

    caplog.set_level(logging.INFO, logger="jobagent.web.app")
    caplog.handler.addFilter(RequestIdFilter())
    web.put("/api/profile/summary", json={"summary": SUMMARY})
    res = web.post("/api/coach", json={"jd_text": JD}, headers={"X-Request-ID": "coach-1"})
    assert done(web, res)["status"] == "done"
    for _ in range(50):
        usage = [r for r in caplog.records if "AI usage" in r.getMessage()]
        if usage:
            break
        time.sleep(0.02)
    (record,) = usage
    assert "calls=" in record.getMessage()
    assert record.request_id == "coach-1"  # type: ignore[attr-defined]


# --- shutdown ---


def test_shutdown_refuses_new_tasks_and_fails_the_running_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, entered = threading.Event(), threading.Event()

    def blocking_discover(*_: Any, **__: Any) -> Any:
        entered.set()
        release.wait(5)
        raise RuntimeError("released after the test")

    monkeypatch.setattr("jobagent.web.app.discover_all", blocking_discover)
    with client_for(settings, shutdown_wait_s=0.2) as c:
        c.post("/api/login", json={"password": PASSWORD})
        c.put("/api/profile/summary", json={"summary": SUMMARY})
        assert c.post("/api/discover").json()["started"] is True
        assert entered.wait(5)
    try:
        res = c.post("/api/coach", json={"jd_text": JD})
        assert res.status_code == 503 and "shutting down" in res.json()["detail"]
        last = Store(settings.db_path).runs()["last"]
        assert last is not None
        assert last["status"] == "failed" and last["error"] == "server shutting down"
    finally:
        release.set()


# --- PDF builder env and upload cap ---


def test_tectonic_sees_only_a_minimal_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(args: list[str], **kw: Any) -> Any:
        seen.update(kw)
        Path(args[args.index("--outdir") + 1], "resume.pdf").write_bytes(b"%PDF-fake")
        return type("Done", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setenv("GEMINI_API_KEY", "leak-me-not")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "leak-me-not")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setattr("jobagent.web.app.shutil.which", lambda _: "/usr/bin/tectonic")
    monkeypatch.setattr("jobagent.web.app.subprocess.run", fake_run)
    assert web_app.compile_pdf("x") == b"%PDF-fake"
    assert set(seen["env"]) <= {"PATH", "HOME", "TMPDIR", "LANG"}
    assert "leak-me-not" not in seen["env"].values()
    assert seen["env"]["LANG"] == "C.UTF-8"


def test_uploaded_summary_over_100k_chars_is_refused(web: TestClient) -> None:
    res = web.post("/api/profile/summary-file", files={"file": ("me.txt", b"x" * 100_001)})
    assert res.status_code == 422 and "100,000" in res.json()["detail"]
    ok = web.post("/api/profile/summary-file", files={"file": ("me.txt", b"x" * 100_000)})
    assert ok.status_code == 200


@pytest.fixture
def web(settings: Settings) -> TestClient:
    c = client_for(settings)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return c
