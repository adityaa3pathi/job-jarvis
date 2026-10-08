"""SQLite storage: jobs, links, applications and status events (R1, R14)."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jobagent.dedupe import (
    dedupe_key,
    normalize_company,
    normalize_title,
    normalize_url,
)
from jobagent.models import JobStatus, Posting
from jobagent.rules import check_hard_rules

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
  location TEXT NOT NULL, experience_text TEXT NOT NULL, stack_words TEXT NOT NULL,
  description TEXT NOT NULL, posted_date TEXT, seen_date TEXT NOT NULL, source TEXT NOT NULL,
  track TEXT, status TEXT NOT NULL, drop_reason TEXT, score INTEGER, rank_reason TEXT
);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs(key);
CREATE TABLE IF NOT EXISTS job_links (
  job_id INTEGER NOT NULL REFERENCES jobs(id), source TEXT NOT NULL, url TEXT NOT NULL,
  norm_url TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
  norm_company TEXT NOT NULL, norm_title TEXT NOT NULL, status TEXT NOT NULL,
  resume_path TEXT, answers_used TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(norm_company, norm_title)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, at TEXT NOT NULL,
  old_status TEXT, new_status TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
"""

Outcome = Literal["new", "merged", "updated", "duplicate"]


@dataclass(frozen=True)
class UpsertResult:
    job_id: int
    outcome: Outcome
    status: str
    drop_reason: str | None = None


@dataclass(frozen=True)
class ApplicationResult:
    application_id: int
    created: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path | str, max_years_asked: int = 5) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        self.conn = sqlite3.connect(str(path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        # WAL lets `job-agent review` read while a pass writes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        self.max_years_asked = max_years_asked

    def close(self) -> None:
        self.conn.close()

    def upsert_posting(self, posting: Posting, now: str | None = None) -> UpsertResult:
        seen = now or _now()
        norm = normalize_url(posting.url)
        row = self.conn.execute(
            "SELECT j.* FROM jobs j JOIN job_links l ON l.job_id=j.id WHERE l.norm_url=?", (norm,)
        ).fetchone()
        if row:
            return self._update_existing(row, posting, seen)
        key = dedupe_key(posting.company, posting.title, posting.location)
        row = self.conn.execute("SELECT * FROM jobs WHERE key=? LIMIT 1", (key,)).fetchone()
        if row:
            with self.conn:
                self._add_link(row["id"], posting)
                self.conn.execute("UPDATE jobs SET seen_date=? WHERE id=?", (seen, row["id"]))
            return UpsertResult(row["id"], "merged", row["status"], row["drop_reason"])
        reason = check_hard_rules(posting, self.max_years_asked)
        status = "dropped" if reason else "found"
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO jobs(key, company, title, location, experience_text, stack_words,"
                " description, posted_date, seen_date, source, status, drop_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key, posting.company, posting.title, posting.location, posting.experience_text,
                    json.dumps(posting.stack_words), posting.description,
                    posting.posted_date.isoformat() if posting.posted_date else None,
                    seen, posting.source, status, reason,
                ),
            )  # fmt: skip
            job_id = int(cur.lastrowid or 0)
            self._add_link(job_id, posting)
            self._event(job_id, None, status, reason or "")
        return UpsertResult(job_id, "new", status, reason)

    def _add_link(self, job_id: int, posting: Posting) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO job_links(job_id, source, url, norm_url) VALUES (?,?,?,?)",
            (job_id, posting.source, normalize_url(posting.url), normalize_url(posting.url)),
        )

    def _update_existing(self, row: sqlite3.Row, posting: Posting, seen: str) -> UpsertResult:
        new_date = posting.posted_date.isoformat() if posting.posted_date else None
        reposted = bool(new_date and new_date != row["posted_date"])
        with self.conn:
            if reposted:
                self.conn.execute(
                    "UPDATE jobs SET posted_date=?, seen_date=?, description=?, title=? WHERE id=?",
                    (new_date, seen, posting.description or row["description"], posting.title,
                     row["id"]),
                )  # fmt: skip
            else:
                self.conn.execute("UPDATE jobs SET seen_date=? WHERE id=?", (seen, row["id"]))
            if posting.closed and row["status"] not in ("closed", "applied"):
                self._set_status(row["id"], "closed", "source says closed")
        outcome: Outcome = "updated" if reposted else "duplicate"
        status = self.get_job(row["id"])["status"]
        return UpsertResult(row["id"], outcome, status, row["drop_reason"])

    def _event(self, job_id: int, old: str | None, new: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events(job_id, at, old_status, new_status, note) VALUES (?,?,?,?,?)",
            (job_id, _now(), old, new, note),
        )

    def _set_status(self, job_id: int, status: JobStatus, note: str = "") -> None:
        old = self.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        self._event(job_id, old, status, note)

    def set_status(self, job_id: int, status: JobStatus, note: str = "") -> None:
        with self.conn:
            self._set_status(job_id, status, note)

    def record_rank(
        self,
        job_id: int,
        *,
        track: str | None,
        score: int | None,
        reason: str,
        drop_reason: str | None = None,
    ) -> None:
        """Store a ranking. A drop moves the job to dropped; a score moves it to ranked."""
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET track=?, score=?, rank_reason=?,"
                " drop_reason=COALESCE(?, drop_reason) WHERE id=?",
                (track, score, reason, drop_reason, job_id),
            )
            if drop_reason:
                self._set_status(job_id, "dropped", drop_reason)
            elif score is not None:
                self._set_status(job_id, "ranked", f"score {score}")

    def get_job(self, job_id: int) -> sqlite3.Row:
        row: sqlite3.Row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row

    def links(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT source, url FROM job_links WHERE job_id=? ORDER BY rowid", (job_id,)
        ).fetchall()

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()

    def create_application(self, job_id: int) -> ApplicationResult:
        """Return the existing application for this company and role instead of a second one."""
        job = self.get_job(job_id)
        company, title = normalize_company(job["company"]), normalize_title(job["title"])
        existing = self.conn.execute(
            "SELECT id FROM applications WHERE norm_company=? AND norm_title=?", (company, title)
        ).fetchone()
        if existing:
            return ApplicationResult(existing["id"], False)
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO applications(job_id, norm_company, norm_title, status, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?)",
                (job_id, company, title, "found", now, now),
            )
        return ApplicationResult(int(cur.lastrowid or 0), True)
