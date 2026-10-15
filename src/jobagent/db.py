"""SQLite storage: jobs, links, applications and status events (R1, R14)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, get_args

from jobagent.classify import city_of, role_of, work_mode_of
from jobagent.dedupe import (
    dedupe_key,
    normalize_company,
    normalize_title,
    normalize_url,
)
from jobagent.models import DropReason, JobFilters, JobStatus, Posting
from jobagent.rules import RULES_VERSION, check_hard_rules, years_asked
from jobagent.sqlconn import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
  location TEXT NOT NULL, experience_text TEXT NOT NULL, stack_words TEXT NOT NULL,
  description TEXT NOT NULL, posted_date TEXT, seen_date TEXT NOT NULL, source TEXT NOT NULL,
  track TEXT, status TEXT NOT NULL, drop_reason TEXT, score INTEGER, rank_reason TEXT,
  rules_version INTEGER NOT NULL DEFAULT 1, missed_runs INTEGER NOT NULL DEFAULT 0,
  min_years INTEGER, max_years INTEGER, role TEXT NOT NULL DEFAULT '',
  work_mode TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs(key);
CREATE INDEX IF NOT EXISTS jobs_status_source ON jobs(status, source);
CREATE INDEX IF NOT EXISTS jobs_status_posted ON jobs(status, posted_date);
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
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS job_links_job ON job_links(job_id);
CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# SCHEMA holds the first release only; every later column, table or index on a new column
# is a numbered step in MIGRATIONS, so an old table is altered before anything uses it.
# Indexes a page read cannot do without; checked at app startup (efficiency audit #1).
INDEXES = {"job_links_job": "CREATE INDEX IF NOT EXISTS job_links_job ON job_links(job_id)"}

# Migration 1: columns added after the first release.
NEW_COLUMNS = {
    "rules_version": "INTEGER NOT NULL DEFAULT 1",
    "missed_runs": "INTEGER NOT NULL DEFAULT 0",
    "min_years": "INTEGER",
    "max_years": "INTEGER",
    "role": "TEXT NOT NULL DEFAULT ''",
    "work_mode": "TEXT NOT NULL DEFAULT ''",
    "city": "TEXT NOT NULL DEFAULT ''",
}
DERIVED = ("min_years", "max_years", "role", "work_mode", "city")
DERIVED_FROM = ("title", "location", "experience_text", "description")
# Drops the hard rules made, so a rule fix may undo them; owner and ranker drops stay.
RULE_REASONS = frozenset(get_args(DropReason)) - {"no_track"}
OWNER = "owner"  # the event note of a status the owner set; rules never undo it
SWEPT = re.compile(r"gone from \d+ clean runs")
# A field of a job's latest status event; merge events only repeat the status.
LAST_EVENT = (
    "(SELECT e.{} FROM events e WHERE e.job_id=j.id AND e.note NOT LIKE 'merged job %'"
    " ORDER BY e.id DESC LIMIT 1)"
)
LAST_NOTE, LAST_AT = LAST_EVENT.format("note"), LAST_EVENT.format("at")
POSITIVE = frozenset({"applied", "interview"})  # owner marks that count as a yes label
RANK_TEXT_CHARS = 1500  # the embedded head of a posting (U14)
JUDGE_TEXT_CHARS = 4000
PRUNE_AFTER_DAYS = 30  # owner, 15-10-2026: dropped jobs keep their text this long (R4)
RULES_FLAG = "lists_rules_version"  # the rules the stored list validators were read under
STALE_CLAIM = timedelta(minutes=30)  # a 'sending' digest older than this died mid-send
# Every jobs column but the text: a batch preload reads its length and hash instead (KTD4).
LEAN = ", ".join(
    f"j.{c}" for c in (
        "id", "key", "company", "title", "location", "experience_text", "stack_words",
        "posted_date", "seen_date", "source", "track", "status", "drop_reason", "score",
        "rank_reason", "rules_version", "missed_runs", "min_years", "max_years", "role",
        "work_mode", "city", "desc_hash", "pruned_at",
    )
) + ", length(j.description) AS desc_len"  # fmt: skip

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


IST = timezone(timedelta(hours=5, minutes=30))
DAY = "substr(COALESCE(NULLIF(j.posted_date, ''), j.seen_date), 1, 10)"
# The id tie-break keeps pages from overlapping or skipping when sort keys are equal.
ORDERS = {
    "newest": f"{DAY} DESC, j.id DESC",
    "fewest_years": f"j.min_years IS NULL, j.min_years, {DAY} DESC, j.id DESC",
    "best": f"j.score IS NULL, j.score DESC, {DAY} DESC, j.id DESC",
}
JOB_COLUMNS = (
    "j.id, j.company, j.title, j.location, j.experience_text, j.posted_date, j.seen_date,"
    " j.source, j.status, j.drop_reason, j.score, j.rank_reason,"
    " (SELECT url FROM job_links l WHERE l.job_id=j.id ORDER BY rowid LIMIT 1) AS url,"
    " j.min_years, j.max_years, j.role, j.work_mode, j.city"
)

FULL_LIST_SOURCES = frozenset({"greenhouse", "lever", "ashby", "workable", "smartrecruiters"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _chunks(items: list[str]) -> list[list[str]]:
    return [items[i : i + CHUNK] for i in range(0, len(items), CHUNK)]


def today_ist() -> date:
    return datetime.now(IST).date()


def bump_version(conn: Any) -> None:
    """Call inside the write's own transaction, so readers never see new data on an old tag."""
    conn.execute(
        "INSERT INTO flags(name, value) VALUES ('data_version', '1') ON CONFLICT(name)"
        " DO UPDATE SET value=CAST(value AS INTEGER) + 1"
    )


def data_version(conn: Any) -> int:
    """Changes whenever anything a page shows changes (KTD1); run progress does not."""
    row = conn.execute("SELECT value FROM flags WHERE name='data_version'").fetchone()
    return int(row["value"]) if row else 0


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _add_column(conn: Any, name: str, kind: str, table: str = "jobs") -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name in have:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    except Exception as exc:  # another handle added it after our read
        if "duplicate column" not in str(exc).lower():
            raise
    conn.commit()


def hash_missing(conn: Any) -> int:
    """Fill `desc_hash` a chunk at a time, so the text never sits in memory all at once."""
    done = 0
    sql = "SELECT id, description FROM jobs WHERE desc_hash IS NULL LIMIT ?"
    while rows := conn.execute(sql, (CHUNK,)).fetchall():
        with conn:
            conn.executemany(
                "UPDATE jobs SET desc_hash=? WHERE id=?",
                [(text_hash(r["description"]), r["id"]) for r in rows],
            )
        done += len(rows)
    return done


def _m1_columns(conn: Any) -> None:
    for name, kind in NEW_COLUMNS.items():
        _add_column(conn, name, kind)


def _m2_lean_text(conn: Any) -> None:
    _add_column(conn, "desc_hash", "TEXT")
    _add_column(conn, "pruned_at", "TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fetch_cache (url TEXT PRIMARY KEY, etag TEXT,"
        " last_modified TEXT, body_sha256 TEXT NOT NULL, saved_at TEXT NOT NULL)"
    )
    conn.commit()
    hash_missing(conn)


RANK_SCHEMA = """
CREATE INDEX IF NOT EXISTS jobs_status_score ON jobs(status, score);
CREATE TABLE IF NOT EXISTS labels (
  job_id INTEGER PRIMARY KEY, label INTEGER NOT NULL, status TEXT NOT NULL, role TEXT NOT NULL,
  source TEXT NOT NULL, company TEXT NOT NULL, work_mode TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
  key TEXT PRIMARY KEY, model TEXT NOT NULL, vec BLOB NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
  run_id INTEGER PRIMARY KEY, status TEXT NOT NULL, jobs INTEGER NOT NULL DEFAULT 0,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_jobs (job_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL);
"""


def _m3_ranking(conn: Any) -> None:
    for name in ("judge_score INTEGER", "judge_reason TEXT", "judged_on TEXT"):
        _add_column(conn, *name.split(" "))
    for statement in filter(str.strip, RANK_SCHEMA.split(";")):
        conn.execute(statement)
    conn.commit()


def judge_key(desc_hash: str | None, profile: str) -> str:
    """What a verdict was made on: the posting text and the profile."""
    return f"{desc_hash or ''}:{text_hash(profile)}"


def _m4_judge_key_and_digest_progress(conn: Any) -> None:
    _add_column(conn, "judge_key", "TEXT")
    _add_column(conn, "sent_chunks", "INTEGER NOT NULL DEFAULT 0", table="digests")
    # Verdicts made before keys existed are taken as made on today's profile.
    with conn:
        conn.execute(
            "UPDATE jobs SET judge_key=coalesce(desc_hash, '') || ':' || ?"
            " WHERE judged_on IS NOT NULL AND judge_key IS NULL",
            (text_hash(_profile_text(conn)),),
        )


# Run once each, in order; a step must be safe to re-run (two handles may open at once).
MIGRATIONS: tuple[Callable[[Any], None], ...] = (
    _m1_columns, _m2_lean_text, _m3_ranking, _m4_judge_key_and_digest_progress,
)  # fmt: skip


def schema_version(conn: Any) -> int:
    row = conn.execute("SELECT value FROM flags WHERE name='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def migrate(conn: Any) -> None:
    done = schema_version(conn)
    for number, step in enumerate(MIGRATIONS, 1):
        if number <= done:
            continue
        step(conn)
        with conn:
            conn.execute(
                "INSERT INTO flags(name, value) VALUES ('schema_version', ?) ON CONFLICT(name)"
                " DO UPDATE SET value=excluded.value"
                " WHERE CAST(value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                (str(number),),
            )


def _profile_text(conn: Any) -> str:
    """The owner's career summary plus every confirmed profile fact (U13), when those
    tables exist in this database; empty when there is no profile yet."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    parts: list[str] = []
    if "profile" in have:
        row = conn.execute("SELECT summary FROM profile WHERE id=1").fetchone()
        parts += [row[0]] if row and row[0] else []
    if "profile_facts" in have:
        rows = conn.execute("SELECT text FROM profile_facts WHERE confirmed=1 ORDER BY id")
        parts += [r[0] for r in rows.fetchall()]
    return "\n".join(parts).strip()


def _like(text: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"


def _where(f: JobFilters, *, by_source: bool = True) -> tuple[str, list[object]]:
    """SQL conditions for the filters, values bound as parameters only (KTD3)."""
    parts: list[str] = ["1=1"]
    args: list[object] = []
    for col in ("status", "source", "role", "work_mode", "city", "company"):
        value = getattr(f, col, None)
        if value is not None and (by_source or col != "source"):
            nocase = " COLLATE NOCASE" if col == "company" else ""
            parts.append(f"j.{col}=?{nocase}")
            args.append(value)
    if f.max_years is not None:
        unstated = " OR j.min_years IS NULL" if f.include_unstated else ""
        parts.append(f"(j.min_years<=?{unstated})")
        args.append(f.max_years)
    if f.posted_within_days:  # days, not hours: "1" is today or yesterday in IST
        parts.append(f"{DAY}>=?")
        args.append((today_ist() - timedelta(days=f.posted_within_days)).isoformat())
    if f.q:
        cols = ("j.title", "j.company", "j.description")
        parts.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")")
        args += [_like(f.q)] * len(cols)
    return " AND ".join(parts), args


def derive(job: Mapping[str, Any]) -> dict[str, object]:
    """The filter columns of a job (KTD1): years asked, role group, work mode and city."""
    text = {c: job[c] for c in DERIVED_FROM}
    asked = years_asked(Posting.model_construct(**text))
    return {
        "min_years": asked[0] if asked else None, "max_years": asked[1] if asked else None,
        "role": role_of(job["title"]), "work_mode": work_mode_of(job["location"], job["title"]),
        "city": city_of(job["location"]),
    }  # fmt: skip


def _stale_derived(job: Mapping[str, Any]) -> dict[str, object]:
    return {c: v for c, v in derive(job).items() if job[c] != v}


class Database:
    def __init__(self, path: Path | str, max_years_asked: int = 5, *, schema: bool = True) -> None:
        self.conn = connect(path)
        if schema:
            self.conn.executescript(SCHEMA)
            migrate(self.conn)
        self.max_years_asked = max_years_asked

    def close(self) -> None:
        self.conn.close()

    def data_version(self) -> int:
        return data_version(self.conn)

    def ensure_indexes(self) -> list[str]:
        """Create any missing index in INDEXES; returns the names that were missing."""
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master").fetchall()}
        missing = [name for name in INDEXES if name not in have]
        with self.conn:
            for name in missing:
                self.conn.execute(INDEXES[name])
        return missing

    def schema_version(self) -> int:
        return schema_version(self.conn)

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
            if out:
                bump_version(self.conn)
        return out

    def _known(
        self, keyed: list[tuple[Posting, str, str]]
    ) -> tuple[dict[str, _Job], dict[str, _Job]]:
        by_url: dict[str, _Job] = {}
        for chunk in _chunks(sorted({norm for _, norm, _ in keyed})):
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                f"SELECT l.norm_url AS link_url, {LAST_NOTE} AS last_note, {LEAN} FROM jobs j"
                f" JOIN job_links l ON l.job_id=j.id WHERE l.norm_url IN ({marks})", chunk,
            ).fetchall():  # fmt: skip
                by_url[row["link_url"]] = dict(row)
        by_key: dict[str, _Job] = {}
        keys = sorted({key for _, norm, key in keyed if norm not in by_url})
        for chunk in _chunks(keys):
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT {LAST_NOTE} AS last_note, {LEAN} FROM jobs j WHERE key IN ({marks})"
            sql += " ORDER BY id"
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
            "desc_hash": text_hash(posting.description),
        }  # fmt: skip
        row |= derive(row)
        cur = self.conn.execute(
            f"INSERT INTO jobs({', '.join(row)}) VALUES ({','.join('?' * len(row))})",
            tuple(row.values()),
        )
        job_id = int(cur.lastrowid or 0)
        if not self._add_link(job_id, posting, norm):
            # Another handle saved this URL after our read: keep its row, not a linkless copy.
            self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            found = self.conn.execute(
                f"SELECT {LEAN} FROM jobs j JOIN job_links l ON l.job_id=j.id WHERE l.norm_url=?",
                (norm,),
            ).fetchone()
            by_url[norm] = dict(found)
            return self._refresh(by_url[norm], posting, seen, bumps)
        self._event(job_id, None, status, reason or "")
        row |= {"id": job_id, "missed_runs": 0, "pruned_at": None}
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

    def _with_text(self, job: _Job) -> _Job:
        """The job with its description, read now only if the batch preload left it out."""
        if "description" in job:
            return job
        got = self.conn.execute("SELECT description FROM jobs WHERE id=?", (job["id"],)).fetchone()
        return job | {"description": got["description"] if got else ""}

    def _refresh(
        self, row: _Job, posting: Posting, seen: str, bumps: list[tuple[str, int]]
    ) -> UpsertResult:
        """A known job seen again: take richer text, re-run stale or new rules, honour closed,
        reopen a job the sweep closed. A status the owner set is never re-judged; a pruned
        job keeps its blank text and its verdict (U4)."""
        pruned = bool(row["pruned_at"])
        new_date = posting.posted_date.isoformat() if posting.posted_date else None
        reposted = bool(new_date and new_date != row["posted_date"])
        changes: dict[str, object] = {"seen_date": seen}
        if reposted:
            changes |= {"posted_date": new_date, "title": posting.title}
        has_text = len(row["description"]) if "description" in row else row["desc_len"]
        if not pruned and posting.description and (reposted or not has_text):
            digest = text_hash(posting.description)  # same text: nothing to write
            if digest != row["desc_hash"]:
                changes |= {"description": posting.description, "desc_hash": digest}
        if not pruned and posting.experience_text and not row["experience_text"]:
            changes["experience_text"] = posting.experience_text
        # A changed description is always new text: it is only set when its hash differs.
        moved = {col for col, value in changes.items() if value != row.get(col)}
        richer = bool(moved & {"title", "description", "experience_text"})
        stale = (
            row["status"] == "dropped" and row["drop_reason"] in RULE_REASONS
            and row["rules_version"] < RULES_VERSION
        )  # fmt: skip
        events: list[tuple[str, str, str]] = []
        if row["status"] == "closed" and SWEPT.fullmatch(row.get("last_note") or ""):
            changes["status"] = "found"
            events.append(("closed", "found", "seen again"))
        found = changes.get("status", row["status"]) == "found"
        if not pruned and row.get("last_note") != OWNER and (stale or (richer and found)):
            verdict, event = self._judge(self._with_text(row | changes))
            changes |= verdict
            events += [event] if event else []
        status = str(changes.get("status", row["status"]))
        if posting.closed and status not in ("closed", "applied"):
            changes["status"] = "closed"
            events.append((status, "closed", "source says closed"))
        # Only new text or a never-derived row can change them; recheck backfills rule changes.
        if not pruned and (row["role"] == "" or moved & set(DERIVED_FROM)):
            changes |= _stale_derived(self._with_text(row | changes))  # current rows keep the bump
        if len(changes) == 1:
            bumps.append((seen, row["id"]))
        else:
            changes["missed_runs"] = 0
            sets = ", ".join(f"{col}=?" for col in changes)
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*changes.values(), row["id"]))
        for old, new, note in events:
            self._event(row["id"], old, new, note)
            row["last_note"] = note
        row.update(changes)
        outcome: Outcome = "updated" if reposted else "duplicate"
        return UpsertResult(row["id"], outcome, row["status"], row["drop_reason"])

    def close_vanished(self, postings: Sequence[Posting], *, clean: bool, after_runs: int) -> int:
        """Close found jobs that a portal's clean reads missed `after_runs` times in a row (R8).
        Upserts reset the count; an errored or empty read proves nothing, so counts for none.
        Only companies that listed jobs in this read are swept: an absent board proves nothing."""
        # Only company boards list every open job; a search board shows a window of results.
        postings = [p for p in postings if p.source in FULL_LIST_SOURCES]
        if not clean or not postings:
            return 0
        seen = {normalize_url(p.url) for p in postings}
        boards = {(p.source, normalize_company(p.company)) for p in postings}
        portals = sorted({source for source, _ in boards})
        rows = [
            row for row in self.conn.execute(
                "SELECT j.id, j.source, j.company, j.missed_runs, l.norm_url FROM jobs j"
                " JOIN job_links l ON l.job_id=j.id"
                f" WHERE j.status='found' AND j.source IN ({','.join('?' * len(portals))})",
                portals,
            ).fetchall()
            if (row["source"], normalize_company(row["company"])) in boards
        ]  # fmt: skip
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
                bump_version(self.conn)
        return len(closing)

    def recheck(self) -> RecheckSummary:
        """Re-key rows and links with today's normalisers, merge rows that now collide, then
        re-run the hard rules on found and rule-dropped rows. A second run changes nothing."""
        with self.conn:
            merged = self._rekey()
            self._renorm_applications()
            recheckable = RULE_REASONS - {"closed"}  # no fresh source says it reopened
            sql = f"SELECT {LAST_NOTE} AS last_note, j.* FROM jobs j ORDER BY id"
            every = [dict(row) for row in self.conn.execute(sql).fetchall()]
            rows = [
                row
                for row in every
                if row["last_note"] != OWNER
                and not row["pruned_at"]  # its rules ran on text it no longer has
                and (
                    row["status"] == "found"
                    or (row["status"] == "dropped" and row["drop_reason"] in recheckable)
                )
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
            self._backfill(every)
            self._rehash(every)
            bump_version(self.conn)
        return RecheckSummary(
            rechecked=len(rows),
            undropped=sum(e[2] == "found" for e in events),
            dropped=sum(e[1] == "found" for e in events),
            merged=merged,
        )

    def _backfill(self, rows: list[_Job]) -> None:
        """Derived filter columns on every row, owner-set ones too, in one write (KTD2).
        Pruned rows keep theirs: their text is gone, not changed."""
        live = [row for row in rows if not row["pruned_at"]]
        fresh = [derive(row) | {"id": row["id"]} for row in live if _stale_derived(row)]
        if fresh:
            sets = ", ".join(f"{col}=?" for col in DERIVED)
            self.conn.executemany(
                f"UPDATE jobs SET {sets} WHERE id=?", [tuple(f.values()) for f in fresh]
            )

    def _rehash(self, rows: list[_Job]) -> None:
        """A missing or stale `desc_hash`; a pruned row keeps the hash of its old text."""
        live = [(text_hash(row["description"]), row) for row in rows if not row["pruned_at"]]
        fixes = [(digest, row["id"]) for digest, row in live if digest != row["desc_hash"]]
        if fixes:
            self.conn.executemany("UPDATE jobs SET desc_hash=? WHERE id=?", fixes)

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
        text = () if keep["pruned_at"] else ("description", "experience_text", "desc_hash")
        fill = {c: dup[c] for c in text if not keep[c] and dup[c]}
        early = ("found", "dropped")
        if keep["status"] in early and dup["status"] not in early:  # keep the owner's progress
            fill |= {c: dup[c] for c in ("status", "drop_reason", "track", "score", "rank_reason")}
        if fill:
            sets = ", ".join(f"{col}=?" for col in fill)
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fill.values(), keep["id"]))
            keep.update(fill)
        self.conn.execute("DELETE FROM jobs WHERE id=?", (dup["id"],))
        self._event(keep["id"], keep["status"], keep["status"], f"merged job {dup['id']}")

    def prune(self, days: int = PRUNE_AFTER_DAYS, now: datetime | None = None) -> int:
        """Blank the text of jobs the rules dropped over `days` ago (R4). The hash stays, and
        `pruned_at` keeps them from being refilled or re-judged on text they no longer have."""
        at = now or datetime.now(UTC)
        reasons = sorted(RULE_REASONS)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET description='', pruned_at=? WHERE id IN (SELECT j.id FROM jobs j"
                " WHERE j.status='dropped' AND j.pruned_at IS NULL AND j.description!=''"
                f" AND j.drop_reason IN ({','.join('?' * len(reasons))})"
                f" AND COALESCE({LAST_NOTE}, '')!=? AND {LAST_AT}<?)",
                (at.isoformat(), *reasons, OWNER, (at - timedelta(days=days)).isoformat()),
            )
            pruned = max(cur.rowcount, 0)
            if pruned:
                bump_version(self.conn)
        return pruned

    def known_urls(self, source: str) -> set[str]:
        """Normalised URLs of every job stored from this source."""
        rows = self.conn.execute("SELECT norm_url FROM job_links WHERE source=?", (source,))
        return {row[0] for row in rows.fetchall()}

    def fetch_validators(self) -> dict[str, tuple[str | None, str | None, str]]:
        rows = self.conn.execute(
            "SELECT url, etag, last_modified, body_sha256 FROM fetch_cache"
        ).fetchall()
        return {row[0]: (row[1], row[2], row[3]) for row in rows}

    def lists_need_full_read(self) -> bool:
        """True after a rules bump: an unchanged list is skipped before its jobs reach the
        rules, so every list is read in full once under the new rules."""
        row = self.conn.execute("SELECT value FROM flags WHERE name=?", (RULES_FLAG,)).fetchone()
        return row is None or row["value"] != str(RULES_VERSION)

    def forget_validators(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM fetch_cache")

    def mark_lists_read(self) -> None:
        """Call once a run's saves are done; until then the next run reads in full again."""
        if not self.lists_need_full_read():
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO flags(name, value) VALUES (?, ?) ON CONFLICT(name)"
                " DO UPDATE SET value=excluded.value",
                (RULES_FLAG, str(RULES_VERSION)),
            )

    def save_validators(self, entries: Mapping[str, tuple[str | None, str | None, str]]) -> None:
        """Kept only after a list's jobs are saved, so an unsaved list is read again."""
        if not entries:
            return
        now = _now()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO fetch_cache(url, etag, last_modified, body_sha256, saved_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET etag=excluded.etag,"
                " last_modified=excluded.last_modified, body_sha256=excluded.body_sha256,"
                " saved_at=excluded.saved_at",
                [(url, *v, now) for url, v in entries.items()],
            )

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
        bump_version(self.conn)

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
            bump_version(self.conn)
            if drop_reason:
                self._set_status(job_id, "dropped", drop_reason)
            elif score is not None:
                self._set_status(job_id, "ranked", f"score {score}")

    # --- owner marks, labels and ranking (U14) ---

    def mark(self, job_id: int, status: JobStatus) -> None:
        """An owner mark, plus its training label in the same write: applied or interview
        is a yes, dropped a no, back to found clears it; other marks keep the last label."""
        with self.conn:
            self._set_status(job_id, status, OWNER)
            if status == "found":
                self.conn.execute("DELETE FROM labels WHERE job_id=?", (job_id,))
            elif status in POSITIVE or status == "dropped":
                job = self.conn.execute(
                    "SELECT role, source, company, work_mode FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                self.conn.execute(
                    "INSERT INTO labels(job_id, label, status, role, source, company, work_mode,"
                    " at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET"
                    " label=excluded.label, status=excluded.status, at=excluded.at",
                    (job_id, int(status in POSITIVE), status, job["role"], job["source"],
                     normalize_company(job["company"]), job["work_mode"], _now()),
                )  # fmt: skip

    def labels(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM labels ORDER BY job_id").fetchall()
        return [dict(r) for r in rows]

    def rank_rows(self) -> list[dict[str, Any]]:
        """Every found job with the head of its text: all the cheap score reads."""
        rows = self.conn.execute(
            "SELECT id, title, company, source, role, work_mode, min_years, desc_hash, score,"
            " rank_reason, judge_score, judge_reason, judge_key, stack_words,"
            f" substr(description, 1, {RANK_TEXT_CHARS}) AS head FROM jobs WHERE status='found'"
        ).fetchall()
        return [dict(r) for r in rows]

    def save_ranks(self, rows: Sequence[tuple[int | None, str, int]]) -> None:
        """(score, reason, id) rows; one version bump for the whole batch."""
        if not rows:
            return
        with self.conn:
            self.conn.executemany("UPDATE jobs SET score=?, rank_reason=? WHERE id=?", rows)
            bump_version(self.conn)

    def judge_candidates(self, limit: int, profile: str) -> list[dict[str, Any]]:
        """Found jobs the AI judge never saw, or saw with other posting text or another
        profile, best cheap score first."""
        rows = self.conn.execute(
            "SELECT id, title, location, desc_hash, score, rank_reason,"
            f" substr(description, 1, {JUDGE_TEXT_CHARS}) AS head FROM jobs"
            " WHERE status='found' AND score IS NOT NULL AND (judged_on IS NULL"
            " OR judge_key IS NOT coalesce(desc_hash, '') || ':' || ?)"
            " ORDER BY score DESC, id LIMIT ?",
            (text_hash(profile), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def judged_on(self, day: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE judged_on=?", (day,)).fetchone()
        return int(row[0])

    def save_judgement(
        self, job_id: int, *, day: str, cap: int | None, reason: str, score: int | None,
        rank_reason: str, key: str,
    ) -> None:  # fmt: skip
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET judged_on=?, judge_score=?, judge_reason=?, score=?,"
                " rank_reason=?, judge_key=? WHERE id=?",
                (day, cap, reason, score, rank_reason, key, job_id),
            )
            bump_version(self.conn)

    def vectors(self, keys: Sequence[str]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for chunk in _chunks(sorted(set(keys))):
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT key, vec FROM embeddings WHERE key IN ({marks})"
            out |= {r["key"]: bytes(r["vec"]) for r in self.conn.execute(sql, chunk).fetchall()}
        return out

    def save_vectors(self, model: str, items: Mapping[str, bytes]) -> None:
        if not items:
            return
        now = _now()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO embeddings(key, model, vec, at) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO NOTHING",
                [(key, model, vec, now) for key, vec in items.items()],
            )

    def profile_text(self) -> str:
        return _profile_text(self.conn)

    # --- the daily digest (U15) ---

    def claim_digest(self, run_id: int) -> bool:
        """Only one caller at a time may send a run's digest. A failed send leaves it
        'partial' for a retry; a 'sending' claim older than STALE_CLAIM died mid-send."""
        now = datetime.now(UTC)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO digests(run_id, status, at) VALUES (?, 'sending', ?)"
                " ON CONFLICT(run_id) DO UPDATE SET status='sending', at=excluded.at"
                " WHERE digests.status='partial'"
                " OR (digests.status='sending' AND digests.at < ?)",
                (run_id, now.isoformat(), (now - STALE_CLAIM).isoformat()),
            )
        return cur.rowcount > 0

    def digest_progress(self, run_id: int) -> tuple[int, int]:
        """(messages, jobs) of this run's digest already sent."""
        row = self.conn.execute(
            "SELECT sent_chunks, jobs FROM digests WHERE run_id=?", (run_id,)
        ).fetchone()
        return (int(row["sent_chunks"]), int(row["jobs"])) if row else (0, 0)

    def digest_chunk_sent(self, run_id: int, job_ids: Sequence[int]) -> None:
        """Recorded after each message, so a retry resumes after the last one sent."""
        with self.conn:
            self.conn.execute(
                "UPDATE digests SET sent_chunks=sent_chunks + 1, jobs=jobs + ?, at=?"
                " WHERE run_id=?",
                (len(job_ids), _now(), run_id),
            )
            self.conn.executemany(
                "INSERT INTO digest_jobs(job_id, run_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                [(i, run_id) for i in job_ids],
            )

    def release_digest(self, run_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM digests WHERE run_id=? AND status='sending' AND sent_chunks=0",
                (run_id,),
            )
            self.conn.execute(
                "UPDATE digests SET status='partial' WHERE run_id=? AND status='sending'",
                (run_id,),
            )

    def finish_digest(self, run_id: int, job_ids: Sequence[int] = ()) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO digest_jobs(job_id, run_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                [(i, run_id) for i in job_ids],
            )
            self.conn.execute(
                "UPDATE digests SET status='sent', at=?,"
                " jobs=(SELECT COUNT(*) FROM digest_jobs WHERE run_id=?) WHERE run_id=?",
                (_now(), run_id, run_id),
            )

    def digest_jobs(self, limit: int) -> list[dict[str, Any]]:
        """Best-scored found jobs no earlier digest has shown."""
        rows = self.conn.execute(
            "SELECT j.id, j.title, j.company, j.location, j.score, j.rank_reason,"
            " (SELECT url FROM job_links l WHERE l.job_id=j.id ORDER BY rowid LIMIT 1) AS url"
            " FROM jobs j WHERE j.status='found' AND j.score IS NOT NULL"
            " AND j.id NOT IN (SELECT job_id FROM digest_jobs)"
            " ORDER BY j.score DESC, j.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_job(self, job_id: int) -> sqlite3.Row:
        row: sqlite3.Row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row

    def list_jobs(
        self, *, status: str | None = None, source: str | None = None, limit: int = 200
    ) -> list[sqlite3.Row]:
        """Newest first, any status unless given; kept for older callers."""
        filters = JobFilters(source=source).model_copy(update={"status": status})
        return self.query_jobs(filters, limit=limit, offset=0)[0]

    def query_jobs(
        self, filters: JobFilters, *, limit: int, offset: int
    ) -> tuple[list[sqlite3.Row], int]:
        """One page of matching jobs, with the first link of each, and the match count."""
        where, args = _where(filters)
        order = ORDERS[filters.sort]
        rows = self.conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs j WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        total = self.conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", args).fetchone()
        return rows, int(total[0])

    def source_counts(self, filters: JobFilters) -> dict[str, int]:
        """Matches per source under every filter but the source, for the portal tabs (R1)."""
        where, args = _where(filters, by_source=False)
        rows = self.conn.execute(
            f"SELECT j.source, COUNT(*) AS n FROM jobs j WHERE {where} GROUP BY j.source", args
        ).fetchall()
        return {row["source"]: int(row["n"]) for row in rows}

    def facets(self, status: str) -> dict[str, list[dict[str, Any]]]:
        """Companies (top 200) and cities (top 60) with jobs in this status, by count."""
        out: dict[str, list[dict[str, Any]]] = {}
        for name, col, top in (("companies", "company", 200), ("cities", "city", 60)):
            rows = self.conn.execute(
                f"SELECT {col} AS name, COUNT(*) AS n FROM jobs WHERE status=? AND {col}!=''"
                f" GROUP BY {col} ORDER BY n DESC, {col} LIMIT ?",
                (status, top),
            ).fetchall()
            out[name] = [{"name": row["name"], "count": int(row["n"])} for row in rows]
        return out

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
