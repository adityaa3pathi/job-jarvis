"""The Turso path: the same storage code must work over a libsql connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import libsql
import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.db import Database
from jobagent.models import Posting
from jobagent.sqlconn import LibsqlConnection
from tests import test_web


@pytest.fixture(autouse=True)
def use_libsql(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_connect(path: Path | str) -> Any:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return LibsqlConnection(libsql.connect(str(path)))

    monkeypatch.setattr("jobagent.db.connect", fake_connect)
    monkeypatch.setattr("jobagent.web.store.connect", fake_connect)


def test_jobs_store_reads_rows_by_name_and_position(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    assert not isinstance(db.conn, sqlite3.Connection)
    posting = Posting(source="hirist", url="https://www.hirist.tech/j/a-1", company="Acme",
                      title="Full Stack Developer", location="Pune")  # fmt: skip
    saved = db.upsert_posting(posting)
    assert db.upsert_posting(posting).outcome == "duplicate"
    db.set_status(saved.job_id, "applied", "owner")
    (row,) = db.list_jobs(status="applied")
    assert (row["company"], row["url"]) == ("Acme", "https://hirist.tech/j/a-1")
    assert dict(row)["title"] == "Full Stack Developer"
    db.close()


def test_failed_write_is_rolled_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with pytest.raises(ValueError), db.conn:
        db.conn.execute("INSERT INTO events(job_id, at, new_status) VALUES (1, 'x', 'found')")
        raise ValueError("boom")
    assert db.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0
    db.close()


def test_full_coach_flow_over_libsql(settings: Settings) -> None:
    c: TestClient = test_web.client_for(settings)
    c.post("/api/login", json={"password": test_web.PASSWORD})
    test_web.test_chat_saves_history_and_facts_wait_for_confirmation(c)
    test_web.test_resume_build_uses_confirmed_facts_and_returns_tex(c)
