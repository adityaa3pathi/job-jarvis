"""Storage for the web app: profile, coach sessions, chat, facts, resume builds, discovery
runs and the kill switch. Runs and the switch live here so they survive restarts (KTD5)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jobagent.lock import STALE_AFTER_S
from jobagent.resume.jd import JDProfile, SkillNeed
from jobagent.runs import read_kill_switch, write_kill_switch
from jobagent.sqlconn import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1), summary TEXT NOT NULL DEFAULT '',
  base_tex TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coach_sessions (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', jd_text TEXT NOT NULL,
  profile_json TEXT NOT NULL, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES coach_sessions(id),
  role TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES coach_sessions(id),
  text TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resume_builds (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES coach_sessions(id),
  tex TEXT NOT NULL, report TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
  status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', counts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
RunStatus = Literal["running", "done", "failed"]
STALE_RUN_ERROR = "run did not finish (server restarted or stuck for 30 min)"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def profile_to_json(profile: JDProfile) -> str:
    return json.dumps(asdict(profile))


def profile_from_json(raw: str) -> JDProfile:
    data = json.loads(raw)
    data["skills"] = [SkillNeed(**s) for s in data["skills"]]
    data["implicit_skills"] = [SkillNeed(**s) for s in data["implicit_skills"]]
    return JDProfile(**data)


def _run_view(row: sqlite3.Row, now: float) -> dict[str, Any]:
    view = {
        **json.loads(row["counts_json"]), "id": row["id"], "status": row["status"],
        "started_at": row["started_at"], "finished_at": row["finished_at"], "error": row["error"],
    }  # fmt: skip
    if view["status"] == "running" and now - row["started_at"] >= STALE_AFTER_S:
        view.update(status="failed", error=STALE_RUN_ERROR)
    return view


class Store:
    def __init__(self, path: Path | str, *, schema: bool = True) -> None:
        self.conn = connect(path)
        if schema:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def profile(self) -> dict[str, str]:
        row = self.conn.execute("SELECT summary, base_tex, updated_at FROM profile").fetchone()
        return dict(row) if row else {"summary": "", "base_tex": "", "updated_at": ""}

    def save_profile(self, *, summary: str | None = None, base_tex: str | None = None) -> None:
        current = self.profile()
        with self.conn:
            self.conn.execute(
                "INSERT INTO profile(id, summary, base_tex, updated_at) VALUES (1,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET summary=excluded.summary,"
                " base_tex=excluded.base_tex, updated_at=excluded.updated_at",
                (
                    current["summary"] if summary is None else summary,
                    current["base_tex"] if base_tex is None else base_tex,
                    _now(),
                ),
            )

    def add_session(
        self, *, url: str, title: str, company: str, jd_text: str, profile: str, report: str
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO coach_sessions(created_at, source_url, title, company, jd_text,"
                " profile_json, report_json) VALUES (?,?,?,?,?,?,?)",
                (_now(), url, title, company, jd_text, profile, report),
            )
        return int(cur.lastrowid or 0)

    def sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, created_at, source_url, title, company FROM coach_sessions ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def session(self, session_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM coach_sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row

    def add_message(self, session_id: int, role: str, text: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO messages(session_id, role, text, at) VALUES (?,?,?,?)",
                (session_id, role, text, _now()),
            )

    def messages(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, text, at FROM messages WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def add_fact(self, session_id: int, text: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO facts(session_id, text, at) VALUES (?,?,?)",
                (session_id, text, _now()),
            )

    def facts(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, text, confirmed FROM facts WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
        return [{**dict(r), "confirmed": bool(r["confirmed"])} for r in rows]

    def set_fact(self, session_id: int, fact_id: int, confirmed: bool) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE facts SET confirmed=? WHERE id=? AND session_id=?",
                (int(confirmed), fact_id, session_id),
            )
        return cur.rowcount == 1

    def add_build(self, session_id: int, tex: str, report: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO resume_builds(session_id, tex, report, at) VALUES (?,?,?,?)",
                (session_id, tex, report, _now()),
            )
        return int(cur.lastrowid or 0)

    def builds(self, session_id: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT b.id, b.session_id, b.at, s.title, s.company FROM resume_builds b"
            " JOIN coach_sessions s ON s.id=b.session_id"
        )
        args: tuple[int, ...] = ()
        if session_id is not None:
            sql, args = sql + " WHERE b.session_id=?", (session_id,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY b.id DESC", args).fetchall()]

    def build(self, build_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM resume_builds WHERE id=?", (build_id,)
        ).fetchone()
        return row

    # --- kill switch ---

    def kill_switch(self) -> bool:
        return read_kill_switch(self.conn)

    def set_kill_switch(self, on: bool) -> None:
        write_kill_switch(self.conn, on)

    # --- discovery runs ---

    def start_run(self, started_at: float | None = None) -> int:
        """Call while holding the run lock: any other "running" row is a leftover."""
        now = time.time() if started_at is None else started_at
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status='failed', error=?, finished_at=? WHERE status='running'",
                (STALE_RUN_ERROR, now),
            )
            cur = self.conn.execute(
                "INSERT INTO runs(started_at, status) VALUES (?, 'running')", (now,)
            )
        return int(cur.lastrowid or 0)

    def update_run(self, run_id: int, counts: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET counts_json=? WHERE id=?", (json.dumps(counts), run_id)
            )

    def finish_run(
        self, run_id: int, status: RunStatus, error: str, counts: dict[str, Any]
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status=?, error=?, finished_at=?, counts_json=? WHERE id=?",
                (status, error, time.time(), json.dumps(counts), run_id),
            )

    def failed_run(self, error: str) -> None:
        """A run that never started (e.g. the lock is held) is still shown to the owner."""
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(started_at, finished_at, status, error) VALUES (?,?,'failed',?)",
                (now, now, error),
            )

    def runs(self, now: float | None = None) -> dict[str, dict[str, Any] | None]:
        """The run still going, if any, and the last finished one. Refused starts (lock held)
        may be newer than the live run, so both are picked from the latest few rows."""
        now = time.time() if now is None else now
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10").fetchall()
        views = [_run_view(r, now) for r in rows]
        current = next((v for v in views if v["status"] == "running"), None)
        last = next((v for v in views if v["status"] != "running"), None)
        return {"current": current, "last": last}
