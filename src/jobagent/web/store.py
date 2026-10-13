"""Storage for the web app: profile, coach sessions, chat, facts and resume builds."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobagent.resume.jd import JDProfile, SkillNeed

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
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def profile_to_json(profile: JDProfile) -> str:
    return json.dumps(asdict(profile))


def profile_from_json(raw: str) -> JDProfile:
    data = json.loads(raw)
    data["skills"] = [SkillNeed(**s) for s in data["skills"]]
    data["implicit_skills"] = [SkillNeed(**s) for s in data["implicit_skills"]]
    return JDProfile(**data)


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        # Web requests may open and use a connection on different worker threads, never at once.
        self.conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
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
