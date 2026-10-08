from __future__ import annotations

from datetime import date

import pytest

from jobagent.db import Database
from jobagent.dedupe import normalize_city, normalize_company, normalize_url
from jobagent.models import Posting
from jobagent.rules import check_hard_rules, parse_years


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


def posting(**kw: object) -> Posting:
    base = {
        "source": "greenhouse",
        "url": "https://boards.greenhouse.io/acme/jobs/1?gh_src=abc",
        "company": "Acme Technologies Pvt Ltd",
        "title": "AI Engineer",
        "location": "Bengaluru",
        "experience_text": "2-3 yrs",
    }
    base.update(kw)
    return Posting.model_validate(base)


def test_same_job_from_two_sources_merges(db: Database) -> None:
    first = db.upsert_posting(posting(source="linkedin", url="https://linkedin.com/jobs/view/9"))
    second = db.upsert_posting(
        posting(company="ACME", location="Bangalore, India", title="AI Engineer (Remote)")
    )
    assert second.outcome == "merged"
    assert second.job_id == first.job_id
    assert [r["source"] for r in db.links(first.job_id)] == ["linkedin", "greenhouse"]
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_seen_twice_is_not_readded(db: Database) -> None:
    a = db.upsert_posting(posting())
    b = db.upsert_posting(posting(url="https://boards.greenhouse.io/acme/jobs/1/"))
    assert b.outcome == "duplicate"
    assert b.job_id == a.job_id
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_application_is_never_created_twice(db: Database) -> None:
    a = db.upsert_posting(posting(source="lever", url="https://jobs.lever.co/acme/x"))
    first = db.create_application(a.job_id)
    second = db.create_application(a.job_id)
    assert first.created and not second.created
    assert first.application_id == second.application_id
    # Same role under a different city is a separate job but must not be applied to twice.
    other = db.upsert_posting(posting(url="https://x.example/other", location="Pune"))
    assert other.outcome == "new"
    assert not db.create_application(other.job_id).created


def test_repost_updates_existing_row(db: Database) -> None:
    a = db.upsert_posting(posting(posted_date=date(2026, 9, 1)))
    b = db.upsert_posting(posting(posted_date=date(2026, 10, 1)))
    assert b.outcome == "updated"
    assert b.job_id == a.job_id
    assert db.get_job(a.job_id)["posted_date"] == "2026-10-01"
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_whatsapp_contact_dropped_with_reason(db: Database) -> None:
    res = db.upsert_posting(
        posting(description="Interested? WhatsApp your resume to +91 98xxxxxx now.")
    )
    assert res.status == "dropped"
    assert res.drop_reason == "whatsapp_contact"
    assert db.get_job(res.job_id)["drop_reason"] == "whatsapp_contact"


def test_whatsapp_integration_in_stack_is_not_dropped() -> None:
    p = posting(description="Build WhatsApp Business API integrations in Python.")
    assert check_hard_rules(p) is None


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"location": "San Francisco, CA"}, "outside_india"),
        ({"location": "Remote - US"}, "outside_india"),
        ({"experience_text": "5+ years"}, "too_senior"),
        ({"experience_text": "6-9 yrs"}, "too_senior"),
        ({"title": "Senior AI Engineer", "experience_text": ""}, "too_senior"),
        ({"description": "Unpaid internship, no stipend."}, "unpaid"),
        ({"description": "Pay a registration fee to apply"}, "spam"),
        ({"closed": True}, "closed"),
    ],
)
def test_hard_rules(kw: dict[str, object], reason: str) -> None:
    assert check_hard_rules(posting(**kw)) == reason


@pytest.mark.parametrize(
    "kw",
    [
        {"experience_text": "3-7 yrs overall, asks 1-3 yrs hands-on"},
        {"location": "Remote (India)"},
        {"location": ""},
        {"experience_text": "0-1 yr"},
    ],
)
def test_hard_rules_keep(kw: dict[str, object]) -> None:
    assert check_hard_rules(posting(**kw)) is None


def test_years_parse() -> None:
    assert parse_years("5+ years") == (5, None)
    assert parse_years("2-6 yrs") == (2, 6)
    assert parse_years("3 to 5 years") == (3, 5)
    assert parse_years("fresher") is None


def test_normalisers() -> None:
    assert (
        normalize_url("HTTPS://Jobs.Lever.co/Acme/x/?utm=1#top") == "https://jobs.lever.co/Acme/x"
    )
    assert normalize_company("Acme Technologies Pvt. Ltd.") == "acme"
    assert normalize_city("Bangalore, India") == "bengaluru"


def test_status_change_recorded(db: Database) -> None:
    res = db.upsert_posting(posting())
    db.set_status(res.job_id, "ranked", "score 4")
    assert [e["new_status"] for e in db.events(res.job_id)] == ["found", "ranked"]
