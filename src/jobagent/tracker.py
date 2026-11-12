"""The owner's applications: stage, dates, interview time, notes and follow-up reminders.
Stages are job statuses (models.TRACKED); the rest lives on the `applications` row."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from jobagent.db import IST, LAST_AT, Database, bump_version
from jobagent.models import TRACKED

FOLLOW_UP_DAYS = 7  # owner, 12-11-2026: no reply a week after applying means follow up
_UNSET: Any = object()

VIEW = (
    "SELECT j.id, j.company, j.title, j.status, j.score, j.city, j.location,"
    " (SELECT url FROM job_links l WHERE l.job_id=j.id ORDER BY rowid LIMIT 1) AS url,"
    " (SELECT MIN(e.at) FROM events e WHERE e.job_id=j.id"
    f"   AND e.new_status IN ({','.join('?' * len(TRACKED))})) AS applied_at,"
    f" {LAST_AT} AS changed_at, a.interview_at, a.note, a.followed_up_at"
    " FROM jobs j LEFT JOIN applications a"
    "   ON a.id=(SELECT MAX(id) FROM applications WHERE job_id=j.id)"
    f" WHERE j.status IN ({','.join('?' * len(TRACKED))})"
)


def _day(value: str | None) -> date | None:
    """The IST calendar day of a stored ISO time."""
    if not value:
        return None
    at = datetime.fromisoformat(value)
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(IST).date()


def _view(row: Any, today: date) -> dict[str, Any]:
    out = dict(row)
    out["note"] = out["note"] or ""
    marks = [d for d in (_day(out["applied_at"]), _day(out["followed_up_at"])) if d]
    out["days_waiting"] = (today - max(marks)).days if marks else None
    out["follow_up_due"] = (
        out["status"] == "applied" and (out["days_waiting"] or 0) >= FOLLOW_UP_DAYS
    )
    return out


class TrackerStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.conn = db.conn

    def rows(self, today: date) -> list[dict[str, Any]]:
        """Every tracked job, newest application first."""
        sql = f"{VIEW} ORDER BY coalesce(applied_at, changed_at) DESC, j.id DESC"
        found = self.conn.execute(sql, (*TRACKED, *TRACKED)).fetchall()
        return [_view(r, today) for r in found]

    def update(
        self,
        job_id: int,
        *,
        interview_at: datetime | None = _UNSET,
        note: str = _UNSET,
        followed_up: bool = False,
    ) -> None:
        """Set what was given. The row is shared by reposts of one company and role, so it
        moves to the job the owner is editing."""
        app_id = self.db.create_application(job_id).application_id
        sets: dict[str, Any] = {"job_id": job_id, "updated_at": datetime.now(UTC).isoformat()}
        if interview_at is not _UNSET:
            sets["interview_at"] = interview_at.isoformat() if interview_at else None
        if note is not _UNSET:
            sets["note"] = note
        if followed_up:
            sets["followed_up_at"] = sets["updated_at"]
        cols = ", ".join(f"{c}=?" for c in sets)
        with self.conn:
            self.conn.execute(
                f"UPDATE applications SET {cols} WHERE id=?", (*sets.values(), app_id)
            )
            bump_version(self.conn)

    def reminders(self, today: date) -> dict[str, list[dict[str, Any]]]:
        """Follow-ups due, and interviews today or tomorrow (IST), for the daily message."""
        rows = self.rows(today)
        soon = {today, today + timedelta(days=1)}
        interviews = [
            r for r in rows if r["status"] == "interview" and _day(r["interview_at"]) in soon
        ]
        interviews.sort(key=lambda r: str(r["interview_at"]))
        follow = [r for r in rows if r["follow_up_due"]]
        follow.sort(key=lambda r: -int(r["days_waiting"] or 0))
        return {"interviews": interviews, "follow_ups": follow}
