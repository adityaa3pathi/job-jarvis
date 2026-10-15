"""Storage for the web app: profile, coach sessions, chat, facts, resume builds, discovery
runs and the kill switch. Runs and the switch live here so they survive restarts (KTD5)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from jobagent.db import bump_version, data_version
from jobagent.llm import Usage
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
CREATE TABLE IF NOT EXISTS profile_facts (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'claim',
  confirmed INTEGER NOT NULL DEFAULT 0, source_session_id INTEGER, created_at TEXT NOT NULL
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
            self._move_session_facts()

    def _move_session_facts(self) -> None:
        """U13 migration: per-session facts (the old `facts` table) move to the profile.
        Insert and delete share one transaction, so a rerun never copies a row twice."""
        if not self.conn.execute("SELECT 1 FROM facts LIMIT 1").fetchone():
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO profile_facts(text, type, confirmed, source_session_id, created_at)"
                " SELECT text, 'claim', confirmed, session_id, at FROM facts ORDER BY id"
            )
            self.conn.execute("DELETE FROM facts")
            bump_version(self.conn)

    def close(self) -> None:
        self.conn.close()

    def data_version(self) -> int:
        return data_version(self.conn)

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
            bump_version(self.conn)

    def add_session(
        self, *, url: str, title: str, company: str, jd_text: str, profile: str, report: str
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO coach_sessions(created_at, source_url, title, company, jd_text,"
                " profile_json, report_json) VALUES (?,?,?,?,?,?,?)",
                (_now(), url, title, company, jd_text, profile, report),
            )
            bump_version(self.conn)
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
            bump_version(self.conn)

    def messages(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, text, at FROM messages WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- owner profile facts (U13): one store for every session, always pending at first ---

    def add_fact(self, session_id: int, text: str, kind: str = "claim") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO profile_facts(text, type, source_session_id, created_at)"
                " VALUES (?,?,?,?)",
                (text, kind, session_id, _now()),
            )
            bump_version(self.conn)

    def facts(self, session_id: int) -> list[dict[str, Any]]:
        """Facts first proposed in this session, in the shape the session page shows."""
        rows = self.conn.execute(
            "SELECT id, text, confirmed FROM profile_facts WHERE source_session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [{**dict(r), "confirmed": bool(r["confirmed"])} for r in rows]

    def profile_facts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM profile_facts ORDER BY id").fetchall()
        return [{**dict(r), "confirmed": bool(r["confirmed"])} for r in rows]

    def confirmed_facts(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT text FROM profile_facts WHERE confirmed=1 ORDER BY id"
        ).fetchall()
        return [str(r["text"]) for r in rows]

    def set_fact(self, session_id: int, fact_id: int, confirmed: bool) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE profile_facts SET confirmed=? WHERE id=? AND source_session_id=?",
                (int(confirmed), fact_id, session_id),
            )
            bump_version(self.conn)
        return cur.rowcount == 1

    def delete_fact(self, fact_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM profile_facts WHERE id=?", (fact_id,))
            if cur.rowcount != 1:
                return False
            bump_version(self.conn)
        return True

    def add_build(self, session_id: int, tex: str, report: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO resume_builds(session_id, tex, report, at) VALUES (?,?,?,?)",
                (session_id, tex, report, _now()),
            )
            bump_version(self.conn)
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

    # --- AI cache and usage (U5) ---

    _llm_ready = False

    def _llm_tables(self) -> None:
        """Created on first use, so the schema above stays untouched by this section."""
        if not self._llm_ready:
            self.conn.executescript(LLM_SCHEMA)
            self._llm_ready = True

    def cache_get(self, key: str) -> str | None:
        self._llm_tables()
        row = self.conn.execute("SELECT value FROM llm_cache WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def cache_put(self, key: str, step: str, value: str) -> None:
        self._llm_tables()
        with self.conn:
            self.conn.execute(
                "INSERT INTO llm_cache(key, step, value, at) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, at=excluded.at",
                (key, step, value, _now()),
            )

    def add_usage(self, usage: Usage) -> None:
        self._llm_tables()
        with self.conn:
            self.conn.execute(
                "INSERT INTO llm_usage(at, day, step, model, tokens_in, tokens_out,"
                " tokens_cached, ms) VALUES (?,?,?,?,?,?,?,?)",
                (_now(), ist_day(), usage.step, usage.model, usage.tokens_in, usage.tokens_out,
                 usage.tokens_cached, usage.ms),
            )  # fmt: skip

    def usage_today(self, day: str | None = None) -> dict[str, int]:
        """Calls and tokens (in + out) used on one IST day, today by default."""
        self._llm_tables()
        row = self.conn.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens"
            " FROM llm_usage WHERE day=?",
            (day or ist_day(),),
        ).fetchone()
        return {"calls": int(row["calls"]), "tokens": int(row["tokens"])}

    def usage_rows(self, day: str | None = None) -> list[dict[str, Any]]:
        self._llm_tables()
        rows = self.conn.execute(
            "SELECT * FROM llm_usage WHERE day=? ORDER BY id", (day or ist_day(),)
        ).fetchall()
        return [dict(r) for r in rows]


LLM_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, step TEXT NOT NULL, value TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, step TEXT NOT NULL,
  model TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
  tokens_cached INTEGER NOT NULL, ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_day ON llm_usage(day);
"""
IST = timezone(timedelta(hours=5, minutes=30))


def ist_day(now: datetime | None = None) -> str:
    """The budget resets at midnight IST, the owner's day."""
    return (now or datetime.now(UTC)).astimezone(IST).date().isoformat()
