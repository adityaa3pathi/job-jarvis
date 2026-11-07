"""Outreach routes: find people at a company, draft notes, send email on an owner click, and
track replies. LinkedIn is never touched: the owner sends there and marks it here."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from jobagent.config import Settings
from jobagent.db import Database
from jobagent.discovery.base import Fetcher
from jobagent.llm import LLM, short_cause
from jobagent.outreach.draft import (
    FILL,
    Channel,
    SenderProfile,
    draft_for,
    linkedin_search_url,
    text_hash,
)
from jobagent.outreach.emails import domain_of, has_mail_server, valid_email
from jobagent.outreach.facts import Fact, pick_facts
from jobagent.outreach.finder import find_contacts, guess_domain
from jobagent.outreach.gmail import Gmail, GmailError, build_message
from jobagent.outreach.hunter import Hunter
from jobagent.outreach.store import (
    FINAL,
    LINKEDIN_CHECK_DAYS,
    OutreachLimit,
    OutreachStore,
    due_label,
    ist_today,
    max_steps,
)
from jobagent.web.deps import DB
from jobagent.web.store import Store

MAX_TEXT = 5_000
UNFILLED = ("[your name]", "[say why", FILL)  # template gaps the owner must fill


@dataclass
class OutreachDeps:
    settings: Settings
    thinker: Callable[[], LLM]
    board_fetcher: Fetcher
    site_fetcher: Callable[[str], Fetcher]
    gmail: Callable[[], Gmail | None]
    hunter: Callable[[], Hunter | None]
    has_mx: Callable[[str], bool] = has_mail_server


class FindIn(BaseModel):
    domain: str = Field(default="", max_length=200)
    use_hunter: bool = False


class ContactIn(BaseModel):
    name: str = Field(default="", max_length=120)
    title: str = Field(default="", max_length=160)
    email: str = Field(default="", max_length=200)
    email_source: Literal["post", "site", "hunter", "guess", "manual", ""] = "manual"
    linkedin_url: str = Field(default="", max_length=300)


class StartIn(BaseModel):
    job_id: int
    channel: Channel
    domain: str = Field(default="", max_length=200)


class TextIn(BaseModel):
    subject: str = Field(default="", max_length=200)
    body: str = Field(min_length=1, max_length=MAX_TEXT)


class StateIn(BaseModel):
    state: Literal["replied", "no_reply", "stopped", "accepted", "bounced"]


def _view(row: dict[str, Any]) -> dict[str, Any]:
    open_ = row["state"] not in FINAL and int(row["step"]) < max_steps(row["channel"])
    return {
        "id": row["id"], "job_id": row["job_id"], "job_title": row["job_title"] or "",
        "job_url": row["job_url"] or "", "job_status": row["job_status"] or "",
        "company": row["company"], "name": row["name"], "title": row["contact_title"],
        "email": row["email"], "email_source": row["email_source"],
        "linkedin_url": row["linkedin_url"], "contact_id": row["contact_id"],
        "channel": row["channel"], "step": row["step"], "max_steps": max_steps(row["channel"]),
        "state": row["state"], "subject": row["subject"], "body": row["body"],
        "edited": text_hash(row["subject"], row["body"]) != row["draft_hash"],
        "can_send": open_, "next_due": row["next_due"], "first_sent_at": row["first_sent_at"],
        "last_sent_at": row["last_sent_at"], "facts": json.loads(row["facts_json"]),
        "todo": due_label(row) if open_ and row["step"] else "",
        "linkedin_search": linkedin_search_url(row["company"], row["name"] or "hiring manager"),
    }  # fmt: skip


def _job(db: Database, job_id: int) -> dict[str, Any]:
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "no such job")
    return {**dict(row), "urls": [r["url"] for r in db.links(job_id)]}


def add_outreach_routes(
    app: FastAPI, deps: OutreachDeps, *, owner: Any, ai_budget: Callable[[Store], None],
    submit: Callable[[Callable[[Store, threading.Event], dict[str, Any]]], dict[str, str]],
) -> None:  # fmt: skip
    limits = deps.settings.outreach

    def borrow_db() -> Any:
        return app.state.jobs_db.borrow()

    @app.get("/api/outreach", dependencies=[owner])
    def outreach_list(db: DB, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        out = OutreachStore(db.conn)
        return {
            "items": [_view(r) for r in out.all()],
            "stats": out.stats(7),
            "today": {"emails": out.sent_today("email"), "invites": out.sent_today("linkedin"),
                      "jobs": len(out.jobs_started_today())},
            "limits": limits.model_dump(),
            "ready": {"gmail": deps.gmail() is not None, "hunter": deps.hunter() is not None},
            "profile": out.profile().model_dump(),
        }  # fmt: skip

    @app.put("/api/outreach/profile", dependencies=[owner])
    def save_profile(body: SenderProfile, db: DB) -> dict[str, bool]:
        OutreachStore(db.conn).save_profile(body)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/outreach", dependencies=[owner])
    def job_outreach(job_id: int, db: DB, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        job, out = _job(db, job_id), OutreachStore(db.conn)
        return {
            "job": {k: job[k] for k in ("id", "title", "company", "urls")},
            "domain_hint": guess_domain(job["company"], [job["description"]]),
            "contacts": out.contacts(job["company"]),
            "threads": [_view(r) for r in out.for_job(job_id)],
            "limit": limits.people_per_company,
            "linkedin_search": linkedin_search_url(job["company"]),
            "ready": {"gmail": deps.gmail() is not None, "hunter": deps.hunter() is not None},
        }  # fmt: skip

    @app.post("/api/jobs/{job_id}/contacts/find", dependencies=[owner])
    def find(job_id: int, body: FindIn, db: DB) -> dict[str, str]:
        job = _job(db, job_id)
        hunter = deps.hunter() if body.use_hunter else None
        if body.use_hunter and hunter is None:
            raise HTTPException(400, "Hunter is not set up: add HUNTER_API_KEY to the server")

        def work(_: Store, cancelled: threading.Event) -> dict[str, Any]:
            got = find_contacts(
                company=job["company"], description=job["description"], urls=job["urls"],
                domain=body.domain, board_fetcher=deps.board_fetcher,
                site_fetcher=deps.site_fetcher, has_mx=deps.has_mx, hunter=hunter,
            )  # fmt: skip
            return got.model_dump()

        return submit(work)

    @app.post("/api/jobs/{job_id}/contacts", dependencies=[owner])
    def save_contact(job_id: int, body: ContactIn, db: DB) -> dict[str, int]:
        job = _job(db, job_id)
        email = body.email.strip().lower()
        if not (body.name.strip() or email):
            raise HTTPException(400, "give a name or an email")
        if email and not valid_email(email):
            raise HTTPException(400, "that does not look like an email address")
        if email and body.email_source in ("guess", "manual") and not deps.has_mx(domain_of(email)):
            raise HTTPException(400, f"{domain_of(email)} has no mail server; check the address")
        try:
            contact_id = OutreachStore(db.conn).add_contact(
                job["company"], name=body.name.strip(), title=body.title.strip(), email=email,
                email_source=body.email_source, linkedin_url=body.linkedin_url.strip(),
                limit=limits.people_per_company,
            )  # fmt: skip
        except OutreachLimit as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": contact_id}

    @app.delete("/api/contacts/{contact_id}", dependencies=[owner])
    def remove_contact(contact_id: int, db: DB) -> dict[str, bool]:
        if not OutreachStore(db.conn).remove_contact(contact_id):
            raise HTTPException(409, "a message already went to this person, so they stay listed")
        return {"ok": True}

    @app.post("/api/contacts/{contact_id}/outreach", dependencies=[owner])
    def start(contact_id: int, body: StartIn, db: DB) -> dict[str, str]:
        out = OutreachStore(db.conn)
        contact, job = out.contact(contact_id), _job(db, body.job_id)
        if contact is None:
            raise HTTPException(404, "no such contact")
        if body.channel == "email" and not contact["email"]:
            raise HTTPException(400, "this person has no email; use LinkedIn or add an email")
        with app.state.stores.borrow() as st:
            ai_budget(st)
        me = out.profile()
        domain = domain_of(body.domain) if body.domain else domain_of(contact["email"] or "")

        def work(st: Store, cancelled: threading.Event) -> dict[str, Any]:
            site = ""
            if domain:
                try:
                    got = deps.site_fetcher(domain).get(f"https://{domain}")
                    site = got.text if got.status == 200 else ""
                except Exception:  # the facts then come from the job post alone
                    site = ""
            facts = pick_facts(job["description"], site, deps.thinker(), cache=st)
            draft = draft_for(
                body.channel, 1, name=contact["name"], title=job["title"],
                company=job["company"], facts=facts, me=me,
            )  # fmt: skip
            if cancelled.is_set():
                return {}
            with borrow_db() as wdb:
                oid = OutreachStore(wdb.conn).start(
                    body.job_id, contact_id, body.channel,
                    [f.model_dump(by_alias=True) for f in facts], draft,
                )  # fmt: skip
            return {"outreach_id": oid, "facts": len(facts)}

        return submit(work)

    def _row(out: OutreachStore, outreach_id: int) -> dict[str, Any]:
        row = out.get(outreach_id)
        if row is None:
            raise HTTPException(404, "no such outreach")
        return row

    @app.put("/api/outreach/{outreach_id}", dependencies=[owner])
    def save_text(outreach_id: int, body: TextIn, db: DB) -> dict[str, bool]:
        out = OutreachStore(db.conn)
        row = _row(out, outreach_id)
        if row["channel"] == "linkedin" and row["step"] == 0 and len(body.body) > 300:
            raise HTTPException(400, "a LinkedIn connection note can be 300 characters at most")
        out.save_text(outreach_id, body.subject, body.body)
        return {"ok": True}

    def _check_ready(row: dict[str, Any]) -> None:
        if row["state"] in FINAL or row["step"] >= max_steps(row["channel"]):
            raise HTTPException(409, "this conversation is finished; nothing more to send")
        if text_hash(row["subject"], row["body"]) == row["draft_hash"]:
            raise HTTPException(409, "edit the message in your own words before sending")
        if gaps := [g for g in UNFILLED if g in row["body"]]:
            raise HTTPException(409, f"fill in the {gaps[0]} part first")
        if row["step"] > 0 and row["state"] == "waiting" and row["channel"] == "linkedin":
            raise HTTPException(409, "mark the invite as accepted before sending the message")

    def _check_caps(out: OutreachStore, row: dict[str, Any]) -> None:
        if row["step"] > 0:
            return
        kind, cap = (("email", limits.first_emails_per_day) if row["channel"] == "email"
                     else ("linkedin", limits.linkedin_invites_per_day))  # fmt: skip
        if out.sent_today(kind) >= cap:
            raise HTTPException(429, f"today's limit of {cap} first {kind} messages is reached")
        started = out.jobs_started_today()
        if row["job_id"] not in started and len(started) >= limits.jobs_per_day:
            raise HTTPException(429, f"today's limit of {limits.jobs_per_day} jobs is reached")

    def _next(row: dict[str, Any], me: SenderProfile, now: datetime) -> tuple[Any, str | None]:
        """The draft and due date of the step after the one being sent now."""
        step = int(row["step"]) + 1  # the step this send completes
        if step >= max_steps(row["channel"]):
            return None, None
        facts = [Fact.model_validate(f) for f in json.loads(row["facts_json"])]
        draft = draft_for(
            row["channel"], step + 1, name=row["name"], title=row["job_title"] or "",
            company=row["company"], facts=facts, me=me,
        )  # fmt: skip
        if row["channel"] == "linkedin":
            return draft, (ist_today(now) + timedelta(days=LINKEDIN_CHECK_DAYS)).isoformat()
        first = datetime.fromisoformat(row["first_sent_at"]) if row["first_sent_at"] else now
        days = limits.follow_up_days[min(step - 1, len(limits.follow_up_days) - 1)]
        return draft, (ist_today(first) + timedelta(days=days)).isoformat()

    @app.post("/api/outreach/{outreach_id}/send", dependencies=[owner])
    def send(outreach_id: int, db: DB) -> dict[str, Any]:
        """Email: sends now through Gmail. LinkedIn: records that the owner sent it there."""
        out = OutreachStore(db.conn)
        row = _row(out, outreach_id)
        _check_ready(row)
        _check_caps(out, row)
        now = datetime.now(UTC)
        draft, due = _next(row, out.profile(), now)
        if row["channel"] == "linkedin":
            out.record_send(row, next_draft=draft, next_due=due, now=now)
            return {"ok": True, "step": row["step"] + 1}
        gmail = deps.gmail()
        if gmail is None:
            raise HTTPException(400, "Gmail is not connected: see the setup steps on this page")
        try:
            if row["thread_id"] and (state := gmail.reply_state(row["thread_id"])) != "none":
                out.set_state(outreach_id, state)
                raise HTTPException(409, f"they already {state}; no follow-up sent")
            subject = row["subject"]
            if row["step"] > 0 and not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            msg = build_message(row["email"], subject, row["body"], in_reply_to=row["header_id"])
            sent = gmail.send(msg, row["thread_id"])
        except GmailError as exc:
            raise HTTPException(502, f"Gmail: {short_cause(exc)}") from exc
        out.record_send(row, next_draft=draft, next_due=due, thread_id=sent.thread_id,
                        header_id=sent.header_id, now=now)  # fmt: skip
        return {"ok": True, "step": row["step"] + 1}

    @app.post("/api/outreach/{outreach_id}/state", dependencies=[owner])
    def set_state(outreach_id: int, body: StateIn, db: DB) -> dict[str, bool]:
        out = OutreachStore(db.conn)
        row = _row(out, outreach_id)
        if body.state == "accepted":
            if row["channel"] != "linkedin" or row["step"] != 1:
                raise HTTPException(409, "only a sent LinkedIn invite can be accepted")
            out.set_state(outreach_id, "accepted", ist_today().isoformat())
        else:
            out.set_state(outreach_id, body.state)
        return {"ok": True}

    @app.post("/api/outreach/check-replies", dependencies=[owner])
    def check_replies() -> dict[str, str]:
        gmail = deps.gmail()
        if gmail is None:
            raise HTTPException(400, "Gmail is not connected")

        def work(_: Store, cancelled: threading.Event) -> dict[str, Any]:
            counts = {"checked": 0, "replied": 0, "bounced": 0, "errors": 0}
            with borrow_db() as wdb:
                out = OutreachStore(wdb.conn)
                for t in out.waiting_threads():
                    if cancelled.is_set():
                        break
                    try:
                        state = gmail.reply_state(t["thread_id"])
                    except GmailError:
                        counts["errors"] += 1
                        continue
                    counts["checked"] += 1
                    if state != "none":
                        out.set_state(t["id"], state)
                        counts[state] += 1
            return counts

        return submit(work)
