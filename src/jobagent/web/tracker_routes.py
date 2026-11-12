"""Tracker routes: the owner's applications by stage, interview times, notes, follow-ups,
and interview prep for one job."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, Field

from jobagent.llm import LLM, LLMError
from jobagent.models import TRACKED
from jobagent.outreach.store import ist_today
from jobagent.prep import PrepStore, make_prep
from jobagent.resume.coach import with_facts
from jobagent.tracker import TrackerStore
from jobagent.web.deps import DB, ST
from jobagent.web.store import Store

MAX_NOTE = 4_000


class TrackerIn(BaseModel):
    """Only the fields sent are changed; `interview_at: null` clears the time."""

    interview_at: AwareDatetime | None = None
    note: str = Field(default="", max_length=MAX_NOTE)
    followed_up: bool = False


Work = Callable[[Store, threading.Event], dict[str, Any]]


def add_tracker_routes(
    app: FastAPI, *, owner: Any, not_modified: Any, thinker: Callable[[], LLM],
    ai_budget: Callable[[Store], None], submit: Callable[[Work], dict[str, str]],
) -> None:  # fmt: skip
    @app.get("/api/tracker", dependencies=[owner])
    def tracker(db: DB, request: Request, response: Response) -> Any:
        if hit := not_modified(request, response, db.data_version()):
            return hit
        items = TrackerStore(db).rows(ist_today())
        counts = Counter(i["status"] for i in items)
        return {"items": items, "counts": {s: counts[s] for s in TRACKED}}

    @app.patch("/api/tracker/{job_id}", dependencies=[owner])
    def tracker_update(job_id: int, body: TrackerIn, db: DB) -> dict[str, bool]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        if job["status"] not in TRACKED:
            raise HTTPException(409, "mark the job applied first")
        fields = {
            k: getattr(body, k) for k in ("interview_at", "note") if k in body.model_fields_set
        }
        TrackerStore(db).update(job_id, **fields, followed_up=body.followed_up)
        return {"ok": True}

    @app.get("/api/prep/{job_id}", dependencies=[owner])
    def prep_get(job_id: int, db: DB) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        link = db.links(job_id)
        view = {k: job[k] for k in ("id", "title", "company", "status")}
        return {
            "job": view | {"url": link[0]["url"] if link else None},
            "prep": PrepStore(db.conn).latest(job_id),
        }

    @app.post("/api/prep/{job_id}", dependencies=[owner])
    def prep_make(job_id: int, db: DB, st: ST) -> dict[str, str]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        summary = st.profile()["summary"]
        if not summary:
            raise HTTPException(400, "add your career summary on the Profile page first")
        if not job["description"]:
            raise HTTPException(400, "this job has no posting text to prepare from")
        ai_budget(st)
        profile = with_facts(summary, st.confirmed_facts())
        title, company, jd = job["title"], job["company"], job["description"]

        def work(_: Store, cancelled: threading.Event) -> dict[str, Any]:
            try:
                prep = make_prep(title=title, company=company, jd=jd, profile=profile,
                                 llm=thinker())  # fmt: skip
            except LLMError as exc:
                raise HTTPException(502, f"AI step failed: {exc}") from exc
            if cancelled.is_set():
                return {}
            with app.state.jobs_db.borrow() as wdb:
                return {"prep_id": PrepStore(wdb.conn).add(job_id, prep)}

        return submit(work)
