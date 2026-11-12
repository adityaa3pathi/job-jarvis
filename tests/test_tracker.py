from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobagent.config import Settings
from jobagent.db import IST, Database
from jobagent.digest import followup_messages, send_followups
from jobagent.models import Posting
from jobagent.tracker import FOLLOW_UP_DAYS, TrackerStore
from tests.test_web import PASSWORD, client_for


def _job(db: Database, n: int, company: str = "Acme") -> int:
    return db.upsert_posting(
        Posting(source="hirist", url=f"https://www.hirist.tech/j/{n}", company=company,
                title=f"Backend Engineer {n}", location="Bengaluru")
    ).job_id  # fmt: skip


def _applied_days_ago(db: Database, job_id: int, days: int) -> None:
    db.mark(job_id, "applied")
    at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with db.conn:
        db.conn.execute("UPDATE events SET at=? WHERE job_id=?", (at, job_id))


def test_only_tracked_jobs_are_listed_with_their_wait(settings: Settings) -> None:
    db = Database(settings.db_path)
    applied, found = _job(db, 1), _job(db, 2)
    _applied_days_ago(db, applied, 9)
    rows = TrackerStore(db).rows(datetime.now(IST).date())
    assert [r["id"] for r in rows] == [applied] and found not in [r["id"] for r in rows]
    assert rows[0]["days_waiting"] == 9 and rows[0]["follow_up_due"]
    db.close()


def test_following_up_restarts_the_clock(settings: Settings) -> None:
    db = Database(settings.db_path)
    job = _job(db, 1)
    _applied_days_ago(db, job, FOLLOW_UP_DAYS + 3)
    store = TrackerStore(db)
    store.update(job, followed_up=True, note="pinged the recruiter")
    row = store.rows(datetime.now(IST).date())[0]
    assert row["days_waiting"] == 0 and not row["follow_up_due"]
    assert row["note"] == "pinged the recruiter"
    db.close()


def test_a_closed_posting_never_hides_an_interview(settings: Settings) -> None:
    db = Database(settings.db_path)
    job = _job(db, 1)
    db.mark(job, "interview")
    db.upsert_posting(
        Posting(source="hirist", url="https://www.hirist.tech/j/1", company="Acme",
                title="Backend Engineer 1", location="Bengaluru", closed=True)
    )  # fmt: skip
    assert db.get_job(job)["status"] == "interview"
    db.close()


def test_reminders_list_interviews_soon_and_follow_ups(settings: Settings) -> None:
    db = Database(settings.db_path)
    soon, later, chase = _job(db, 1), _job(db, 2), _job(db, 3)
    today = datetime.now(IST).date()
    store = TrackerStore(db)
    for job, day in ((soon, today + timedelta(days=1)), (later, today + timedelta(days=5))):
        db.mark(job, "interview")
        store.update(job, interview_at=datetime(day.year, day.month, day.day, 11, tzinfo=IST))
    _applied_days_ago(db, chase, 8)
    due = store.reminders(today)
    assert [r["id"] for r in due["interviews"]] == [soon]
    assert [r["id"] for r in due["follow_ups"]] == [chase]
    texts = followup_messages([], "12-11-2026", due)
    assert len(texts) == 2 and "11:00 IST" in texts[0] and "waiting 8 days" in texts[1]
    db.close()


def test_daily_message_sends_tracker_reminders_once(settings: Settings) -> None:
    class Sender:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, text: str) -> None:
            self.sent.append(text)

    db, sender = Database(settings.db_path), Sender()
    _applied_days_ago(db, _job(db, 1), 10)
    assert send_followups(db, sender)["follow_ups"] == 1
    assert send_followups(db, sender)["skipped"] == "already sent today"
    assert len(sender.sent) == 1 and "follow up" in sender.sent[0]
    db.close()


def test_tracker_api_lists_and_edits_applications(settings: Settings) -> None:
    db = Database(settings.db_path)
    job, other = _job(db, 1), _job(db, 2)
    db.close()
    web = client_for(settings)
    assert web.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert web.patch(f"/api/tracker/{job}", json={"note": "x"}).status_code == 409
    assert web.post(f"/api/jobs/{job}/mark", json={"status": "interview"}).status_code == 200
    when = "2026-11-20T15:30:00+05:30"
    res = web.patch(f"/api/tracker/{job}", json={"interview_at": when, "note": "system design"})
    assert res.status_code == 200
    assert (
        web.patch(f"/api/tracker/{job}", json={"interview_at": "2026-11-20T15:30"}).status_code
        == 422
    )
    body = web.get("/api/tracker").json()
    assert [i["id"] for i in body["items"]] == [job] and body["counts"]["interview"] == 1
    item = body["items"][0]
    assert item["note"] == "system design"
    assert datetime.fromisoformat(item["interview_at"]).astimezone(IST).hour == 15
    assert web.patch(f"/api/tracker/{job}", json={"interview_at": None}).status_code == 200
    assert web.get("/api/tracker").json()["items"][0]["interview_at"] is None
    assert web.post(f"/api/jobs/{other}/mark", json={"status": "offer"}).status_code == 200
    assert web.get("/api/tracker").json()["counts"]["offer"] == 1
