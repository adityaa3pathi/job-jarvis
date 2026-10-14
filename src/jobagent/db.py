"""SQLite storage: jobs, links, applications and status events (R1, R14)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jobagent.dedupe import (
    dedupe_key,
    normalize_company,
    normalize_title,
    normalize_url,
)
from jobagent.models import JobStatus, Posting
from jobagent.rules import check_hard_rules
from jobagent.sqlconn import connect

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


_Job = dict[str, Any]  # a jobs row read up front, kept current as the batch writes
CHUNK = 500  # stays under SQLite's limit on `?` marks in one statement


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _chunks(items: list[str]) -> list[list[str]]:
    return [items[i : i + CHUNK] for i in range(0, len(items), CHUNK)]


class Database:
    def __init__(self, path: Path | str, max_years_asked: int = 5, *, schema: bool = True) -> None:
        self.conn = connect(path)
        if schema:
            self.conn.executescript(SCHEMA)
        self.max_years_asked = max_years_asked

    def close(self) -> None:
        self.conn.close()

    def upsert_posting(self, posting: Posting, now: str | None = None) -> UpsertResult:
        return self.upsert_many([posting], now)[0]

    def upsert_many(
        self, postings: Sequence[Posting], now: str | None = None
    ) -> list[UpsertResult]:
        """Save one portal's postings in one transaction (R2, R3). Known rows are read up
        front, so a re-seen posting costs one write instead of three calls and a commit."""
        seen = now or _now()
        keyed = [
            (p, normalize_url(p.url), dedupe_key(p.company, p.title, p.location, url=p.url))
            for p in postings
        ]
        by_url, by_key = self._known(keyed)
        bumps: list[tuple[str, int]] = []
        with self.conn:
            out = [
                self._upsert(p, norm, key, seen, by_url, by_key, bumps) for p, norm, key in keyed
            ]
            if bumps:
                self.conn.executemany("UPDATE jobs SET seen_date=? WHERE id=?", bumps)
        return out

    def _known(
        self, keyed: list[tuple[Posting, str, str]]
    ) -> tuple[dict[str, _Job], dict[str, _Job]]:
        by_url: dict[str, _Job] = {}
        for chunk in _chunks(sorted({norm for _, norm, _ in keyed})):
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                "SELECT l.norm_url AS link_url, j.* FROM jobs j JOIN job_links l ON l.job_id=j.id"
                f" WHERE l.norm_url IN ({marks})", chunk,
            ).fetchall():  # fmt: skip
                by_url[row["link_url"]] = dict(row)
        by_key: dict[str, _Job] = {}
        keys = sorted({key for _, norm, key in keyed if norm not in by_url})
        for chunk in _chunks(keys):
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT * FROM jobs WHERE key IN ({marks}) ORDER BY id"
            for row in self.conn.execute(sql, chunk).fetchall():
                by_key.setdefault(row["key"], dict(row))
        return by_url, by_key

    def _upsert(
        self, posting: Posting, norm: str, key: str, seen: str,
        by_url: dict[str, _Job], by_key: dict[str, _Job], bumps: list[tuple[str, int]],
    ) -> UpsertResult:  # fmt: skip
        if row := by_url.get(norm):
            return self._update_existing(row, posting, seen, bumps)
        if row := by_key.get(key):
            self._add_link(row["id"], posting, norm)
            bumps.append((seen, row["id"]))
            by_url[norm] = row
            return UpsertResult(row["id"], "merged", row["status"], row["drop_reason"])
        reason = check_hard_rules(posting, self.max_years_asked)
        status = "dropped" if reason else "found"
        posted = posting.posted_date.isoformat() if posting.posted_date else None
        cur = self.conn.execute(
            "INSERT INTO jobs(key, company, title, location, experience_text, stack_words,"
            " description, posted_date, seen_date, source, status, drop_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key, posting.company, posting.title, posting.location, posting.experience_text,
                json.dumps(posting.stack_words), posting.description, posted,
                seen, posting.source, status, reason,
            ),
        )  # fmt: skip
        job_id = int(cur.lastrowid or 0)
        if not self._add_link(job_id, posting, norm):
            # Another handle saved this URL after our read: keep its row, not a linkless copy.
            self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            found = self.conn.execute(
                "SELECT j.* FROM jobs j JOIN job_links l ON l.job_id=j.id WHERE l.norm_url=?",
                (norm,),
            ).fetchone()
            by_url[norm] = dict(found)
            return self._update_existing(by_url[norm], posting, seen, bumps)
        self._event(job_id, None, status, reason or "")
        row = {"id": job_id, "key": key, "title": posting.title, "description": posting.description,
               "posted_date": posted, "status": status, "drop_reason": reason}  # fmt: skip
        by_url[norm] = row
        by_key.setdefault(key, row)
        return UpsertResult(job_id, "new", status, reason)

    def _add_link(self, job_id: int, posting: Posting, norm: str) -> bool:
        """False when the URL is already linked to a job."""
        cur = self.conn.execute(
            "INSERT INTO job_links(job_id, source, url, norm_url) VALUES (?,?,?,?)"
            " ON CONFLICT(norm_url) DO NOTHING",
            (job_id, posting.source, posting.url, norm),
        )
        return cur.rowcount > 0

    def _update_existing(
        self, row: _Job, posting: Posting, seen: str, bumps: list[tuple[str, int]]
    ) -> UpsertResult:
        new_date = posting.posted_date.isoformat() if posting.posted_date else None
        reposted = bool(new_date and new_date != row["posted_date"])
        closing = posting.closed and row["status"] not in ("closed", "applied")
        changes: dict[str, object] = {"seen_date": seen}
        if reposted:
            changes |= {"posted_date": new_date, "title": posting.title,
                        "description": posting.description or row["description"]}  # fmt: skip
        if closing:
            changes["status"] = "closed"
        if len(changes) == 1:
            bumps.append((seen, row["id"]))
        else:
            sets = ", ".join(f"{col}=?" for col in changes)
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*changes.values(), row["id"]))
        if closing:
            self._event(row["id"], row["status"], "closed", "source says closed")
        row.update(changes)
        outcome: Outcome = "updated" if reposted else "duplicate"
        return UpsertResult(row["id"], outcome, row["status"], row["drop_reason"])

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

    def list_jobs(
        self, *, status: str | None = None, source: str | None = None, limit: int = 200
    ) -> list[sqlite3.Row]:
        """Newest first, with the first link for each job."""
        sql = (
            "SELECT j.id, j.company, j.title, j.location, j.experience_text, j.posted_date,"
            " j.seen_date, j.source, j.status, j.drop_reason, j.score, j.rank_reason,"
            " (SELECT url FROM job_links l WHERE l.job_id=j.id ORDER BY rowid LIMIT 1) AS url"
            " FROM jobs j WHERE 1=1"
        )
        args: list[object] = []
        if status:
            sql, args = sql + " AND j.status=?", [*args, status]
        if source:
            sql, args = sql + " AND j.source=?", [*args, source]
        sql += " ORDER BY COALESCE(j.posted_date, j.seen_date) DESC, j.id DESC LIMIT ?"
        return self.conn.execute(sql, (*args, limit)).fetchall()

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
