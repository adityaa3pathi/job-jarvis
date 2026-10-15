"""One discovery run's bookkeeping, shared by the CLI and the web app: the tally, the
per-portal save step and the kill switch flag (R9, KTD5)."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jobagent.config import Settings
from jobagent.db import Database, UpsertResult
from jobagent.dedupe import normalize_url
from jobagent.digest import Sender, send_digest
from jobagent.discovery.base import DiscoveryResult, SweepContext, Validators
from jobagent.index import Embedder
from jobagent.llm import LLM
from jobagent.models import Posting
from jobagent.policy import Policy
from jobagent.rank import JudgeStore, rank_jobs
from jobagent.sqlconn import connect

log = logging.getLogger(__name__)


@dataclass
class RunTally:
    """What one discovery run read, saved and dropped, per portal (R9)."""

    portals: dict[str, dict[str, int]] = field(default_factory=dict)
    drop_reasons: Counter[str] = field(default_factory=Counter)
    unreachable: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    per_target: dict[str, int] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)

    def add(self, part: DiscoveryResult, saved: Sequence[UpsertResult] | None) -> None:
        name = part.portal or (part.postings[0].source if part.postings else "other")
        row = self.portals.setdefault(name, {"read": 0, "new": 0, "saved": 0})
        row["read"] += len(part.postings)
        for r in saved or []:
            row["saved"] += 1
            if r.outcome == "new" and r.status == "found":
                row["new"] += 1
            elif r.outcome == "new" and r.drop_reason:
                self.drop_reasons[r.drop_reason] += 1
        self.unreachable += [f"{u.company}: {u.reason}" for u in part.unreachable]
        self.skipped += part.skipped_by_policy
        self.notes += part.notes
        self.per_target.update(part.per_target)
        self.unchanged += part.unchanged

    def save(
        self, part: DiscoveryResult, upsert: Callable[[list[Posting]], list[UpsertResult]]
    ) -> list[UpsertResult] | None:
        """Save one portal and count it; a failed save is noted and never loses the others."""
        try:
            saved = upsert(part.postings)
        except Exception as exc:
            log.exception("saving %s failed", part.portal or "a portal")
            self.add(part, None)
            self.note_failure(part, "save", exc)
            return None
        self.add(part, saved)
        return saved

    def note_failure(self, part: DiscoveryResult, what: str, exc: Exception) -> None:
        self.notes.append(
            f"{part.portal or 'a portal'}: {what} failed: {type(exc).__name__}: {exc}"
        )

    def as_dict(self) -> dict[str, Any]:
        totals = {k: sum(r[k] for r in self.portals.values()) for k in ("read", "new", "saved")}
        return {
            **totals, "portals": self.portals,
            "drop_reasons": dict(sorted(self.drop_reasons.items())),
            "unreachable": self.unreachable, "skipped": self.skipped, "notes": self.notes,
            "per_target": self.per_target, "unchanged": len(self.unchanged),
        }  # fmt: skip


class _Jobs(Protocol):
    def upsert_many(self, postings: Sequence[Posting]) -> list[UpsertResult]: ...

    def close_vanished(
        self, postings: Sequence[Posting], *, clean: bool, after_runs: int
    ) -> int: ...

    def save_validators(self, entries: dict[str, Validators]) -> None: ...


class _Runs(Protocol):
    def update_run(self, run_id: int, counts: dict[str, Any]) -> None: ...


def save_portal(
    part: DiscoveryResult, *, db: _Jobs, st: _Runs, run_id: int, tally: RunTally,
    after_runs: int,
) -> list[UpsertResult] | None:  # fmt: skip
    """Save one finished portal. Closing and progress writes are bookkeeping: a failure
    is logged and noted, never raised, so later portals still get saved."""
    saved = tally.save(part, db.upsert_many)
    portal = part.postings[0].source if part.postings else "?"
    log.info("portal %s saved: %d postings", portal, len(part.postings))
    if saved is not None and part.fresh:
        try:
            db.save_validators(part.fresh)
        except Exception as exc:  # those lists are only read again next run
            log.exception("keeping list validators failed")
            tally.note_failure(part, "keeping list validators", exc)
    clean = saved is not None and not (part.unreachable or part.skipped_by_policy or part.partial)
    try:
        db.close_vanished(part.postings, clean=clean, after_runs=after_runs)
    except Exception as exc:
        log.exception("closing vanished jobs failed")
        tally.note_failure(part, "closing vanished jobs", exc)
    try:
        st.update_run(run_id, tally.as_dict())
    except Exception as exc:
        log.exception("recording run progress failed")
        tally.note_failure(part, "recording progress", exc)
    return saved


def sweep_context(db: Database, detail_cap: int) -> SweepContext:
    """Read once before a run: each list's validators and the stored Greenhouse URLs, so
    unchanged lists are skipped and job text is fetched for new jobs only (KTD3)."""
    stored = db.known_urls("greenhouse")
    if db.lists_need_full_read():  # new rules: old validators would skip their re-judging
        db.forget_validators()
    return SweepContext(
        validators={url: Validators(*v) for url, v in db.fetch_validators().items()},
        known=lambda url: normalize_url(url) in stored,
        detail_cap=detail_cap,
    )


def note_policy(policy: Policy, tally: RunTally) -> None:
    """Tell the owner when discovery ran without ever reading the DB kill switch."""
    if policy.stop_unread:
        tally.notes.append("kill switch could not be read")


def rules_swept(db: Database, tally: RunTally) -> None:
    """Once a run's saves are done, its lists were read under today's rules."""
    try:
        db.mark_lists_read()
    except Exception as exc:  # the next run only reads every list in full again
        log.exception("recording the rules version failed")
        tally.notes.append(f"recording the rules version failed: {type(exc).__name__}: {exc}")


def prune_after_run(db: Database, tally: RunTally) -> None:
    """Trim old dropped text once a run's saves are done; a failure is only noted (U4)."""
    try:
        if pruned := db.prune():
            tally.notes.append(f"pruned text of {pruned} old dropped jobs")
    except Exception as exc:
        log.exception("pruning old dropped text failed")
        tally.notes.append(f"pruning failed: {type(exc).__name__}: {exc}")


def rank_after_run(
    db: Database, st: JudgeStore, tally: RunTally, *, embedder: Embedder | None,
    llm: LLM | None, settings: Settings,
) -> None:  # fmt: skip
    """Score every found job once the saves are done (U14); a failure is only noted."""
    try:
        tally.notes.append(rank_jobs(db, st, embedder=embedder, llm=llm, settings=settings))
    except Exception as exc:
        log.exception("ranking failed")
        tally.notes.append(f"ranking failed: {type(exc).__name__}: {exc}")


def digest_after_run(
    db: Database, run_id: int, sender: Sender | None, settings: Settings
) -> dict[str, Any]:
    """Send a finished run's digest (U15). A failure is logged, never raised: the run is
    already saved, and the cron endpoint can send it later."""
    try:
        return send_digest(db, run_id, sender, limit=settings.ranking.digest_top_n)
    except Exception as exc:
        log.exception("digest failed")
        return {"sent": 0, "error": type(exc).__name__}


# --- the kill switch: a DB flag, so it survives restarts ---


def read_kill_switch(conn: Any) -> bool:
    row = conn.execute("SELECT value FROM flags WHERE name='kill_switch'").fetchone()
    return bool(row) and row["value"] == "1"


def write_kill_switch(conn: Any, on: bool) -> None:
    with conn:
        conn.execute(
            "INSERT INTO flags(name, value) VALUES ('kill_switch', ?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            ("1" if on else "0",),
        )


def kill_switch_check(path: Path | str) -> Callable[[], bool]:
    """Reads the DB kill switch on its own connection, so any thread may call it."""

    def check() -> bool:
        conn = connect(path)
        try:
            return read_kill_switch(conn)
        finally:
            conn.close()

    return check
