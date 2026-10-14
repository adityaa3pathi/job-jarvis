"""SQLite storage: jobs, links, applications and status events (R1, R14)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from jobagent.dedupe import (
    dedupe_key,
    normalize_company,
    normalize_title,
    normalize_url,
)
from jobagent.models import DropReason, JobStatus, Posting
from jobagent.rules import RULES_VERSION, check_hard_rules
from jobagent.sqlconn import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
  location TEXT NOT NULL, experience_text TEXT NOT NULL, stack_words TEXT NOT NULL,
  description TEXT NOT NULL, posted_date TEXT, seen_date TEXT NOT NULL, source TEXT NOT NULL,
  track TEXT, status TEXT NOT NULL, drop_reason TEXT, score INTEGER, rank_reason TEXT,
  rules_version INTEGER NOT NULL DEFAULT 1, missed_runs INTEGER NOT NULL DEFAULT 0
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

# Added after the first release; old tables get them by ALTER TABLE.
NEW_COLUMNS = {
    "rules_version": "INTEGER NOT NULL DEFAULT 1",
    "missed_runs": "INTEGER NOT NULL DEFAULT 0",
}
# Drops the hard rules made, so a rule fix may undo them; owner and ranker drops stay.
RULE_REASONS = frozenset(get_args(DropReason)) - {"no_track"}

Outcome = Literal["new", "merged", "updated", "duplicate"]


@dataclass(frozen=True)
class UpsertResult:
    job_id: int
    outcome: Outcome
    status: str
    drop_reason: str | None = None


@dataclass(frozen=True)
class RecheckSummary:
    rechecked: int
    undropped: int
    dropped: int
    merged: int


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
            self._add_columns()
        self.max_years_asked = max_years_asked

    def close(self) -> None:
        self.conn.close()

    def _add_columns(self) -> None:
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, kind in NEW_COLUMNS.items():
            if name in have:
                continue
            try:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
            except Exception as exc:  # another handle added it after our read
                if "duplicate column" not in str(exc).lower():
                    raise
        self.conn.commit()

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
                self.conn.executemany(
                    "UPDATE jobs SET seen_date=?, missed_runs=0 WHERE id=?", bumps
                )
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
            return self._refresh(row, posting, seen, bumps)
        if row := by_key.get(key):
            self._add_link(row["id"], posting, norm)
            by_url[norm] = row
            return replace(self._refresh(row, posting, seen, bumps), outcome="merged")
        reason = check_hard_rules(posting, self.max_years_asked)
        status = "dropped" if reason else "found"
        posted = posting.posted_date.isoformat() if posting.posted_date else None
        row = {
            "key": key, "company": posting.company, "title": posting.title,
            "location": posting.location, "experience_text": posting.experience_text,
            "stack_words": json.dumps(posting.stack_words), "description": posting.description,
            "posted_date": posted, "seen_date": seen, "source": posting.source,
            "status": status, "drop_reason": reason, "rules_version": RULES_VERSION,
        }  # fmt: skip
        cur = self.conn.execute(
            f"INSERT INTO jobs({', '.join(row)}) VALUES ({','.join('?' * len(row))})",
            tuple(row.values()),
        )
        job_id = int(cur.lastrowid or 0)
        if not self._add_link(job_id, posting, norm):
            # Another handle saved this URL after our read: keep its row, not a linkless copy.
            self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            found = self.conn.execute(
                "SELECT j.* FROM jobs j JOIN job_links l ON l.job_id=j.id WHERE l.norm_url=?",
                (norm,),
            ).fetchone()
            by_url[norm] = dict(found)
            return self._refresh(by_url[norm], posting, seen, bumps)
        self._event(job_id, None, status, reason or "")
        row |= {"id": job_id, "missed_runs": 0}
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

    def _judge(self, job: _Job) -> tuple[dict[str, object], tuple[str, str, str] | None]:
        """Hard rules on a stored row: the column changes and the status event, if any."""
        reason = check_hard_rules(
            Posting(source=job["source"], url="", company=job["company"], title=job["title"],
                    location=job["location"], experience_text=job["experience_text"],
                    description=job["description"]),
            self.max_years_asked,
        )  # fmt: skip
        status = "dropped" if reason else "found"
        changes: dict[str, object] = {}
        if job["rules_version"] != RULES_VERSION:
            changes["rules_version"] = RULES_VERSION
        if (status, reason) == (job["status"], job["drop_reason"]):
            return changes, None
        note = reason or f"rules v{RULES_VERSION}: clean"
        return changes | {"status": status, "drop_reason": reason}, (job["status"], status, note)

    def _refresh(
        self, row: _Job, posting: Posting, seen: str, bumps: list[tuple[str, int]]
    ) -> UpsertResult:
        """A known job seen again: take richer text, re-run stale or new rules, honour closed."""
        new_date = posting.posted_date.isoformat() if posting.posted_date else None
        reposted = bool(new_date and new_date != row["posted_date"])
        changes: dict[str, object] = {"seen_date": seen}
        if reposted:
            changes |= {"posted_date": new_date, "title": posting.title,
                        "description": posting.description or row["description"]}  # fmt: skip
        for col in ("description", "experience_text"):
            if not row[col] and getattr(posting, col):
                changes.setdefault(col, getattr(posting, col))
        text = ("title", "description", "experience_text")
        richer = any(changes.get(col, row[col]) != row[col] for col in text)
        stale = (
            row["status"] == "dropped" and row["drop_reason"] in RULE_REASONS
            and row["rules_version"] < RULES_VERSION
        )  # fmt: skip
        events: list[tuple[str, str, str]] = []
        if stale or (richer and row["status"] == "found"):
            verdict, event = self._judge(row | changes)
            changes |= verdict
            events += [event] if event else []
        status = str(changes.get("status", row["status"]))
        if posting.closed and status not in ("closed", "applied"):
            changes["status"] = "closed"
            events.append((status, "closed", "source says closed"))
        if len(changes) == 1:
            bumps.append((seen, row["id"]))
        else:
            changes["missed_runs"] = 0
            sets = ", ".join(f"{col}=?" for col in changes)
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*changes.values(), row["id"]))
        for old, new, note in events:
            self._event(row["id"], old, new, note)
        row.update(changes)
        outcome: Outcome = "updated" if reposted else "duplicate"
        return UpsertResult(row["id"], outcome, row["status"], row["drop_reason"])

    def close_vanished(self, postings: Sequence[Posting], *, clean: bool, after_runs: int) -> int:
        """Close found jobs that a portal's clean reads missed `after_runs` times in a row (R8).
        Upserts reset the count; an errored or empty read proves nothing, so counts for none."""
        if not clean or not postings:
            return 0
        seen = {normalize_url(p.url) for p in postings}
        portals = sorted({p.source for p in postings})
        rows = self.conn.execute(
            "SELECT j.id, j.missed_runs, l.norm_url FROM jobs j JOIN job_links l ON l.job_id=j.id"
            f" WHERE j.status='found' AND j.source IN ({','.join('?' * len(portals))})",
            portals,
        ).fetchall()
        hit = {row["id"] for row in rows if row["norm_url"] in seen}
        missed = {row["id"]: row["missed_runs"] + 1 for row in rows if row["id"] not in hit}
        closing = [job_id for job_id, n in missed.items() if n >= after_runs]
        with self.conn:
            if missed:
                self.conn.executemany(
                    "UPDATE jobs SET missed_runs=? WHERE id=?", [(n, i) for i, n in missed.items()]
                )
            if closing:
                self.conn.executemany(
                    "UPDATE jobs SET status='closed' WHERE id=?", [(i,) for i in closing]
                )
                note = f"gone from {after_runs} clean runs"
                self._events([(i, "found", "closed", note) for i in closing])
        return len(closing)

    def recheck(self) -> RecheckSummary:
        """Re-key rows and links with today's normalisers, merge rows that now collide, then
        re-run the hard rules on found and rule-dropped rows. A second run changes nothing."""
        with self.conn:
            merged = self._rekey()
            self._renorm_applications()
            recheckable = RULE_REASONS - {"closed"}  # no fresh source says it reopened
            rows = [
                dict(row)
                for row in self.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
                if row["status"] == "found"
                or (row["status"] == "dropped" and row["drop_reason"] in recheckable)
            ]
            updates: list[tuple[object, ...]] = []
            events: list[tuple[int, str, str, str]] = []
            for row in rows:
                changes, event = self._judge(row)
                if changes:
                    new = row | changes
                    updates.append((new["status"], new["drop_reason"], RULES_VERSION, row["id"]))
                if event:
                    events.append((row["id"], *event))
            if updates:
                self.conn.executemany(
                    "UPDATE jobs SET status=?, drop_reason=?, rules_version=? WHERE id=?", updates
                )
            self._events(events)
        return RecheckSummary(
            rechecked=len(rows),
            undropped=sum(e[2] == "found" for e in events),
            dropped=sum(e[1] == "found" for e in events),
            merged=merged,
        )

    def _rekey(self) -> int:
        jobs = {row["id"]: dict(row) for row in self.conn.execute("SELECT * FROM jobs").fetchall()}
        links = self.conn.execute(
            "SELECT rowid AS rid, job_id, url, norm_url FROM job_links ORDER BY rowid"
        ).fetchall()
        root: dict[int, int] = {}

        def find(i: int) -> int:
            while root.get(i, i) != i:
                i = root[i]
            return i

        def join(a: int, b: int) -> None:
            a, b = find(a), find(b)
            if a != b:
                root[max(a, b)] = min(a, b)  # the older row survives

        first_url: dict[int, str] = {}
        owner: dict[str, int] = {}
        renorm: list[tuple[str, int]] = []
        twins: list[tuple[int]] = []  # one URL linked twice is one link: keep the first
        for link in links:
            first_url.setdefault(link["job_id"], link["url"])
            norm = normalize_url(link["url"])
            if norm in owner:
                join(owner[norm], link["job_id"])
                twins.append((link["rid"],))
                continue
            owner[norm] = link["job_id"]
            if norm != link["norm_url"]:
                renorm.append((norm, link["rid"]))
        rekeyed: list[tuple[str, int]] = []
        by_key: dict[str, int] = {}
        for job_id, job in sorted(jobs.items()):
            key = dedupe_key(job["company"], job["title"], job["location"],
                             url=first_url.get(job_id, ""))  # fmt: skip
            if key != job["key"]:
                rekeyed.append((key, job_id))
            if not key.startswith("url:"):  # URL keys were joined as links above
                join(by_key.setdefault(key, job_id), job_id)
        if twins:
            self.conn.executemany("DELETE FROM job_links WHERE rowid=?", twins)
        if renorm:  # two steps, so swapped values never trip the UNIQUE index
            temp = [(r,) for _, r in renorm]
            self.conn.executemany("UPDATE job_links SET norm_url='~' || rowid WHERE rowid=?", temp)
            self.conn.executemany("UPDATE job_links SET norm_url=? WHERE rowid=?", renorm)
        if rekeyed:
            self.conn.executemany("UPDATE jobs SET key=? WHERE id=?", rekeyed)
        dups = sorted(i for i in jobs if find(i) != i)
        for dup in dups:
            self._merge_into(jobs[find(dup)], jobs[dup])
        return len(dups)

    def _merge_into(self, keep: _Job, dup: _Job) -> None:
        for table in ("job_links", "events", "applications"):
            self.conn.execute(
                f"UPDATE {table} SET job_id=? WHERE job_id=?", (keep["id"], dup["id"])
            )
        fill = {c: dup[c] for c in ("description", "experience_text") if not keep[c] and dup[c]}
        early = ("found", "dropped")
        if keep["status"] in early and dup["status"] not in early:  # keep the owner's progress
            fill |= {c: dup[c] for c in ("status", "drop_reason", "track", "score", "rank_reason")}
        if fill:
            sets = ", ".join(f"{col}=?" for col in fill)
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fill.values(), keep["id"]))
            keep.update(fill)
        self.conn.execute("DELETE FROM jobs WHERE id=?", (dup["id"],))
        self._event(keep["id"], keep["status"], keep["status"], f"merged job {dup['id']}")

    def _renorm_applications(self) -> None:
        apps = self.conn.execute(
            "SELECT a.id, a.norm_company, a.norm_title, j.company, j.title FROM applications a"
            " JOIN jobs j ON j.id=a.job_id ORDER BY a.id"
        ).fetchall()
        taken: set[tuple[str, str]] = set()
        gone: list[tuple[int]] = []
        moved: list[tuple[str, str, int]] = []
        for app in apps:
            pair = (normalize_company(app["company"]), normalize_title(app["title"]))
            if pair in taken:  # the same role applied to twice: keep the older record
                gone.append((app["id"],))
            elif pair != (app["norm_company"], app["norm_title"]):
                moved.append((*pair, app["id"]))
            taken.add(pair)
        if gone:
            self.conn.executemany("DELETE FROM applications WHERE id=?", gone)
        if moved:
            temp = [(m[2],) for m in moved]
            self.conn.executemany("UPDATE applications SET norm_title='~' || id WHERE id=?", temp)
            self.conn.executemany(
                "UPDATE applications SET norm_company=?, norm_title=? WHERE id=?", moved
            )

    def _events(self, rows: Sequence[tuple[int, str, str, str]]) -> None:
        if rows:
            self.conn.executemany(
                "INSERT INTO events(job_id, at, old_status, new_status, note) VALUES (?,?,?,?,?)",
                [(job_id, _now(), old, new, note) for job_id, old, new, note in rows],
            )

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
