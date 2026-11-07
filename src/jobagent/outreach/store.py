"""Contacts and outreach threads, in the jobs database (tables: db.OUTREACH_SCHEMA).
Every send is also logged in `outreach_sends`, which keeps the daily caps and weekly counts
true after the 90-day clean-up blanks names, emails and text."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from jobagent.db import IST, bump_version
from jobagent.dedupe import normalize_company
from jobagent.outreach.draft import (
    EMAIL_STEPS,
    LINKEDIN_STEPS,
    Channel,
    Draft,
    SenderProfile,
    text_hash,
)

State = Literal["draft", "waiting", "accepted", "replied", "bounced", "no_reply", "stopped"]
FINAL = frozenset({"replied", "bounced", "no_reply", "stopped"})
LINKEDIN_CHECK_DAYS = 7  # after an invite, a reminder to look whether they accepted
PROFILE_FLAG = "outreach_profile"

VIEW = (
    "SELECT o.*, c.name, c.title AS contact_title, c.email, c.email_source, c.linkedin_url,"
    " c.company, j.title AS job_title, j.status AS job_status,"
    " (SELECT url FROM job_links l WHERE l.job_id=o.job_id ORDER BY rowid LIMIT 1) AS job_url"
    " FROM outreach o JOIN contacts c ON c.id=o.contact_id LEFT JOIN jobs j ON j.id=o.job_id"
)


class OutreachLimit(Exception):
    """A cap the owner set (people per company, sends per day) would be passed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ist_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(IST).date()


def max_steps(channel: str) -> int:
    return EMAIL_STEPS if channel == "email" else LINKEDIN_STEPS


class OutreachStore:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # --- the owner's own details for every note ---

    def profile(self) -> SenderProfile:
        row = self.conn.execute("SELECT value FROM flags WHERE name=?", (PROFILE_FLAG,)).fetchone()
        return SenderProfile.model_validate_json(row["value"]) if row else SenderProfile()

    def save_profile(self, me: SenderProfile) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO flags(name, value) VALUES (?, ?) ON CONFLICT(name)"
                " DO UPDATE SET value=excluded.value",
                (PROFILE_FLAG, me.model_dump_json()),
            )
            bump_version(self.conn)

    # --- contacts ---

    def contacts(self, company: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE norm_company=? AND cleared_at IS NULL ORDER BY id",
            (normalize_company(company),),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_contact(
        self, company: str, *, name: str, title: str, email: str, email_source: str,
        linkedin_url: str, limit: int,
    ) -> int:  # fmt: skip
        """The same email (or name) at a company is one contact; past `limit` people, refuse."""
        norm = normalize_company(company)
        with self.conn:
            rows = self.conn.execute(
                "SELECT id, name, email FROM contacts WHERE norm_company=? AND cleared_at IS NULL",
                (norm,),
            ).fetchall()
            for r in rows:
                if (email and r["email"] == email) or (name and r["name"].lower() == name.lower()):
                    self.conn.execute(
                        "UPDATE contacts SET title=?, email=?, email_source=?, linkedin_url=?"
                        " WHERE id=?", (title, email, email_source, linkedin_url, r["id"]),
                    )  # fmt: skip
                    bump_version(self.conn)
                    return int(r["id"])
            if len(rows) >= limit:
                raise OutreachLimit(
                    f"already {len(rows)} people saved at {company}; the limit is {limit}"
                )
            cur = self.conn.execute(
                "INSERT INTO contacts(norm_company, company, name, title, email, email_source,"
                " linkedin_url, found_at) VALUES (?,?,?,?,?,?,?,?)",
                (norm, company, name, title, email, email_source, linkedin_url, _now()),
            )
            bump_version(self.conn)
        return int(cur.lastrowid or 0)

    def contact(self, contact_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM contacts WHERE id=? AND cleared_at IS NULL", (contact_id,)
        ).fetchone()
        return dict(row) if row else None

    def remove_contact(self, contact_id: int) -> bool:
        """Only a contact nothing was sent to; sent ones stay for the record."""
        with self.conn:
            sent = self.conn.execute(
                "SELECT 1 FROM outreach WHERE contact_id=? AND step>0", (contact_id,)
            ).fetchone()
            if sent:
                return False
            self.conn.execute("DELETE FROM outreach WHERE contact_id=?", (contact_id,))
            cur = self.conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
            bump_version(self.conn)
        return bool(cur.rowcount == 1)

    # --- outreach threads ---

    def start(self, job_id: int, contact_id: int, channel: Channel, facts: list[dict[str, Any]],
              draft: Draft) -> int:  # fmt: skip
        """One thread per person and channel; asking again returns the existing one."""
        now = _now()
        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM outreach WHERE contact_id=? AND channel=?", (contact_id, channel)
            ).fetchone()
            if row:
                return int(row["id"])
            cur = self.conn.execute(
                "INSERT INTO outreach(job_id, contact_id, channel, facts_json, subject, body,"
                " draft_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, contact_id, channel, json.dumps(facts), draft.subject, draft.body,
                 text_hash(draft.subject, draft.body), now, now),
            )  # fmt: skip
            bump_version(self.conn)
        return int(cur.lastrowid or 0)

    def get(self, outreach_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(f"{VIEW} WHERE o.id=?", (outreach_id,)).fetchone()
        return dict(row) if row else None

    def for_job(self, job_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"{VIEW} WHERE o.job_id=? ORDER BY o.id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def all(self) -> list[dict[str, Any]]:
        sql = f"{VIEW} WHERE c.cleared_at IS NULL ORDER BY coalesce(o.next_due, '9'), o.id DESC"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def save_text(self, outreach_id: int, subject: str, body: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outreach SET subject=?, body=?, updated_at=? WHERE id=?",
                (subject, body, _now(), outreach_id),
            )
            bump_version(self.conn)
        return bool(cur.rowcount == 1)

    def sent_today(self, channel: str, step: int = 1, now: datetime | None = None) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM outreach_sends WHERE channel=? AND step=? AND day=?",
            (channel, step, ist_today(now).isoformat()),
        ).fetchone()
        return int(row[0])

    def jobs_started_today(self, now: datetime | None = None) -> set[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT job_id FROM outreach_sends WHERE step=1 AND day=?",
            (ist_today(now).isoformat(),),
        ).fetchall()
        return {int(r[0]) for r in rows}

    def record_send(
        self, row: dict[str, Any], *, next_draft: Draft | None, next_due: str | None,
        thread_id: str = "", header_id: str = "", now: datetime | None = None,
    ) -> None:  # fmt: skip
        """One message went out: log it, move the step on and load the next draft."""
        at = now or datetime.now(UTC)
        step = int(row["step"]) + 1
        state = "waiting"
        subject = next_draft.subject or row["subject"] if next_draft else row["subject"]
        body = next_draft.body if next_draft else row["body"]
        with self.conn:
            self.conn.execute(
                "UPDATE outreach SET step=?, state=?, thread_id=coalesce(nullif(?, ''), thread_id),"
                " header_id=coalesce(nullif(?, ''), header_id), first_sent_at=coalesce("
                "first_sent_at, ?), last_sent_at=?, next_due=?, subject=?, body=?, draft_hash=?,"
                " updated_at=? WHERE id=?",
                (step, state, thread_id, header_id, at.isoformat(), at.isoformat(), next_due,
                 subject, body, text_hash(subject, body), at.isoformat(), row["id"]),
            )  # fmt: skip
            self.conn.execute(
                "INSERT INTO outreach_sends(outreach_id, job_id, channel, step, day, at)"
                " VALUES (?,?,?,?,?,?)",
                (row["id"], row["job_id"], row["channel"], step, ist_today(at).isoformat(),
                 at.isoformat()),
            )  # fmt: skip
            bump_version(self.conn)

    def set_state(self, outreach_id: int, state: State, next_due: str | None = None) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outreach SET state=?, next_due=?, updated_at=? WHERE id=?",
                (state, None if state in FINAL else next_due, _now(), outreach_id),
            )
            bump_version(self.conn)
        return bool(cur.rowcount == 1)

    def waiting_threads(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, thread_id FROM outreach WHERE channel='email' AND thread_id!=''"
            " AND state='waiting'"
        ).fetchall()
        return [dict(r) for r in rows]

    def due(self, day: date) -> list[dict[str, Any]]:
        """Open threads whose next step is due on or before `day` (IST)."""
        sql = f"{VIEW} WHERE o.state IN ('waiting', 'accepted') AND o.next_due<=?"
        sql += " ORDER BY o.next_due"
        return [dict(r) for r in self.conn.execute(sql, (day.isoformat(),)).fetchall()]

    def stats(self, days: int = 7, now: datetime | None = None) -> dict[str, int]:
        since = (ist_today(now) - timedelta(days=days - 1)).isoformat()
        sent = self.conn.execute(
            "SELECT COUNT(DISTINCT outreach_id) FROM outreach_sends WHERE step=1 AND day>=?",
            (since,),
        ).fetchone()[0]
        replied = self.conn.execute(
            "SELECT COUNT(*) FROM outreach o WHERE o.state='replied' AND o.id IN (SELECT"
            " outreach_id FROM outreach_sends WHERE step=1 AND day>=?)", (since,),
        ).fetchone()[0]  # fmt: skip
        interviews = self.conn.execute(
            "SELECT COUNT(DISTINCT o.job_id) FROM outreach o JOIN jobs j ON j.id=o.job_id"
            " WHERE j.status='interview' AND o.id IN (SELECT outreach_id FROM outreach_sends"
            " WHERE step=1 AND day>=?)", (since,),
        ).fetchone()[0]  # fmt: skip
        return {"days": days, "sent": int(sent), "replied": int(replied),
                "interviews": int(interviews)}  # fmt: skip

    def clear_old(self, days: int, now: datetime | None = None) -> int:
        """Blank people and message text `days` after the contact was saved; counts stay."""
        at = now or datetime.now(UTC)
        cutoff = (at - timedelta(days=days)).isoformat()
        with self.conn:
            old = [r[0] for r in self.conn.execute(
                "SELECT id FROM contacts WHERE cleared_at IS NULL AND found_at<?", (cutoff,)
            ).fetchall()]  # fmt: skip
            if not old:
                return 0
            marks = ",".join("?" * len(old))
            self.conn.execute(
                "UPDATE contacts SET name='', title='', email='', linkedin_url='', cleared_at=?"
                f" WHERE id IN ({marks})", (at.isoformat(), *old),
            )  # fmt: skip
            self.conn.execute(
                "UPDATE outreach SET subject='', body='', facts_json='[]', thread_id='',"
                " header_id='', next_due=NULL, state=CASE WHEN state IN"
                " ('replied','bounced','no_reply','stopped') THEN state ELSE 'stopped' END"
                f" WHERE contact_id IN ({marks})",
                old,
            )  # fmt: skip
            bump_version(self.conn)
        return len(old)


def due_label(row: dict[str, Any]) -> str:
    """What the owner has to do for a due thread, in plain words."""
    if row["channel"] == "linkedin":
        if row["state"] == "accepted":
            return "send the LinkedIn message"
        return "check if they accepted your LinkedIn invite"
    return f"send follow-up {int(row['step'])} of {EMAIL_STEPS - 1}"
