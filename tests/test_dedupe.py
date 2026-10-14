from __future__ import annotations

from datetime import date

import pytest

from jobagent.db import Database
from jobagent.dedupe import dedupe_key, normalize_city, normalize_company, normalize_url
from jobagent.models import Posting
from jobagent.rules import RULES_VERSION, check_hard_rules, parse_years


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
        posting(
            company="ACME Technologies Private Limited",
            location="Bangalore, India",
            title="AI Engineer (Remote)",
        )
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
        ({"description": "This is a volunteer role for a student club."}, "unpaid"),
        ({"description": "Pay a registration fee to apply"}, "spam"),
        ({"closed": True}, "closed"),
        ({"description": "Send your CV on WhatsApp 98xxxxxxxx"}, "whatsapp_contact"),
        ({"description": "Apply on WhatsApp at +91 98765 43210."}, "whatsapp_contact"),
        ({"description": "Message us: wa.me/919876543210"}, "whatsapp_contact"),
        ({"description": "Pay a registration fee of Rs 500"}, "spam"),
        ({"description": "Earn 2000 per day from home."}, "spam"),
        ({"experience_text": "6–9 years"}, "too_senior"),
        ({"experience_text": "", "description": "Need 7+ years experience; Python."}, "too_senior"),
        ({"title": "Tech Lead - Backend", "experience_text": ""}, "too_senior"),
        ({"title": "Lead Engineer", "experience_text": ""}, "too_senior"),
        ({"location": "Austin, TX"}, "outside_india"),
        ({"location": "Indianapolis, IN"}, "outside_india"),
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
        {"description": "Everyone gets 5 paid days off to volunteer."},
        {"description": "Track unpaid invoices and chase payments."},
        {"description": "Paid parental leave, plus unpaid leave when you need it."},
        {"description": "Build features that send messages via WhatsApp Business API"},
        {"description": "Work on WhatsApp chatbot context handling"},
        {"description": "Admin tools for outreach and callback flows on WhatsApp."},
        {"description": "No experience required, you will learn on the job"},
        {"description": "Help merchants reduce payment processing fees"},
        {"description": "Build rent payment and security deposit flows"},
        {"description": "Benefits: up to 30 days leave without pay"},
        {"experience_text": "3–6 years"},
        {"experience_text": "2—4 yrs"},
        {
            "experience_text": "",
            "description": "We have 15 years of experience serving clients. "
            "You need 1-2 years experience.",
        },
        {"title": "Lead Generation Executive", "experience_text": ""},
        {"title": "Software Engineer (Team lead not required)", "experience_text": ""},
        {"location": "Bengaluru, IN"},
        {"location": "Bangalore Urban"},
        {"location": "Chennai, TN"},
    ],
)
def test_hard_rules_keep(kw: dict[str, object]) -> None:
    assert check_hard_rules(posting(**kw)) is None


def test_years_parse() -> None:
    assert parse_years("5+ years") == (5, None)
    assert parse_years("2-6 yrs") == (2, 6)
    assert parse_years("3 to 5 years") == (3, 5)
    assert parse_years("fresher") is None
    assert parse_years("3–6 years") == (3, 6)
    assert parse_years("2 — 4 yrs") == (2, 4)
    assert parse_years("8 years in total; 3+ years exp in Python") == (3, None)
    history = "We have 15 years of experience serving clients. You need 1-2 years experience."
    assert parse_years(history) == (1, 2)


def test_rules_version() -> None:
    assert RULES_VERSION == 2


def test_normalisers() -> None:
    assert (
        normalize_url("HTTPS://Jobs.Lever.co/Acme/x/?utm=1#top") == "https://jobs.lever.co/Acme/x"
    )
    assert normalize_company("Acme Technologies Pvt. Ltd.") == "acme technologies"
    assert normalize_city("Bangalore, India") == "bengaluru"


def test_status_change_recorded(db: Database) -> None:
    res = db.upsert_posting(posting())
    db.set_status(res.job_id, "ranked", "score 4")
    assert [e["new_status"] for e in db.events(res.job_id)] == ["found", "ranked"]


def test_parenthesised_title_text_keeps_jobs_apart(db: Database) -> None:
    backend = db.upsert_posting(posting(title="Software Engineer (Backend)"))
    frontend = db.upsert_posting(
        posting(
            url="https://boards.greenhouse.io/acme/jobs/2", title="Software Engineer (Frontend)"
        )
    )
    assert frontend.outcome == "new"
    assert backend.job_id != frontend.job_id
    assert dedupe_key("Acme", "AI Engineer (Hybrid)", "Pune") == dedupe_key(
        "Acme", "AI Engineer", "Pune"
    )


def test_companyless_postings_never_key_merge(db: Database) -> None:
    a = posting(company="", url="https://wellfound.com/jobs/1", location="Bangalore Urban")
    b = posting(company="", url="https://wellfound.com/jobs/2", location="Bangalore Urban")
    assert dedupe_key(a.company, a.title, a.location, url=a.url) != dedupe_key(
        b.company, b.title, b.location, url=b.url
    )
    assert db.upsert_posting(a).job_id != db.upsert_posting(b).job_id


def test_company_suffixes_do_not_join_different_companies() -> None:
    assert normalize_company("Acme Technologies") != normalize_company("Acme Solutions")
    assert normalize_company("Acme Pvt Ltd") == normalize_company("Acme Inc.")
    assert normalize_company("Acme Private Limited") == "acme"


@pytest.mark.parametrize(
    "location",
    [
        "Bangalore Urban",
        "Bengaluru, Karnataka, India",
        "Bangalore",
        "Bengaluru",
        "Hybrid - Bangalore",
    ],
)
def test_city_aliases_fold_to_one_value(location: str) -> None:
    assert normalize_city(location) == "bengaluru"
    assert dedupe_key("Razorpay", "SDE", location) == dedupe_key("Razorpay", "SDE", "Bengaluru")


def test_city_suffixes_and_remote_variants() -> None:
    assert normalize_city("Chennai District") == "chennai"
    assert normalize_city("Gurgaon, Haryana") == normalize_city("Gurugram") == "gurugram"
    remote = {normalize_city(x) for x in ("Remote", "Remote - India", "Remote (India)", "WFH")}
    assert remote == {"remote"}


def test_normalize_url_keeps_id_params_and_drops_tracking() -> None:
    first = normalize_url("https://www.acme.com/careers/job?gh_jid=111")
    assert first != normalize_url("https://www.acme.com/careers/job?gh_jid=222")
    assert first == "https://acme.com/careers/job?gh_jid=111"
    assert (
        normalize_url("http://www.Acme.com/Careers/Job/?utm_source=x&id=7&ref=li&jobId=9")
        == "https://acme.com/Careers/Job?id=7&jobId=9"
    )
    assert normalize_url("https://acme.com/job?utm_source=x") == "https://acme.com/job"
    assert normalize_url("https://acme.com/job?gh_src=a&lever-source=b&trk=c&source=d") == (
        "https://acme.com/job"
    )


HIRIST = {"source": "hirist", "url": "https://www.hirist.tech/j/ai-1", "experience_text": ""}


def test_merge_fills_description_and_reruns_rules(db: Database) -> None:
    first = db.upsert_posting(posting(**HIRIST))
    rich = posting(experience_text="", description="You bring 7+ years experience in Python.")
    merged = db.upsert_posting(rich)
    assert (merged.outcome, merged.job_id) == ("merged", first.job_id)
    assert (merged.status, merged.drop_reason) == ("dropped", "too_senior")
    assert db.get_job(first.job_id)["description"] == rich.description


def test_merge_honours_closed(db: Database) -> None:
    first = db.upsert_posting(posting(**HIRIST))
    rich = posting(experience_text="", description="7+ years experience needed.", closed=True)
    db.upsert_posting(rich)
    job = db.get_job(first.job_id)
    assert (job["status"], job["drop_reason"]) == ("closed", "too_senior")
    assert job["description"] == "7+ years experience needed."
    assert [e["new_status"] for e in db.events(first.job_id)] == ["found", "dropped", "closed"]


def drop(db: Database, job_id: int, reason: str | None, version: int = 1) -> None:
    with db.conn:
        db.conn.execute(
            "UPDATE jobs SET status='dropped', drop_reason=?, rules_version=? WHERE id=?",
            (reason, version, job_id),
        )


def test_old_rule_drop_seen_again_with_clean_text_comes_back(db: Database) -> None:
    clean = posting(description="Benefits include unpaid leave and health cover.")
    saved = db.upsert_posting(clean)
    drop(db, saved.job_id, "unpaid")  # the old unpaid rule matched benefits text
    again = db.upsert_posting(clean)
    assert (again.status, again.drop_reason) == ("found", None)
    job = db.get_job(saved.job_id)
    assert (job["status"], job["drop_reason"], job["rules_version"]) == (
        "found", None, RULES_VERSION,
    )  # fmt: skip
    assert db.events(saved.job_id)[-1]["new_status"] == "found"


def test_current_and_owner_drops_are_not_rechecked_on_resee(db: Database) -> None:
    a = db.upsert_posting(posting())
    b = db.upsert_posting(posting(url="https://boards.greenhouse.io/acme/jobs/2", location="Pune"))
    drop(db, a.job_id, "unpaid", RULES_VERSION)
    db.set_status(b.job_id, "dropped", "owner")
    assert db.upsert_posting(posting()).status == "dropped"
    resee = posting(url="https://boards.greenhouse.io/acme/jobs/2", location="Pune")
    assert db.upsert_posting(resee).status == "dropped"


def test_dropped_rows_keep_their_description(db: Database) -> None:
    res = db.upsert_posting(posting(title="Senior AI Engineer", description="Build agents."))
    assert db.get_job(res.job_id)["description"] == "Build agents."


def stale_pair(db: Database) -> tuple[int, int]:
    """Two rows that the old normalisers kept apart; the current ones see one job."""
    a = db.upsert_posting(posting(**HIRIST))
    b = db.upsert_posting(
        posting(url="https://boards.greenhouse.io/acme/jobs/1?gh_jid=7", location="Pune",
                description="Build RAG pipelines.")
    )  # fmt: skip
    with db.conn:
        db.conn.execute(
            "UPDATE jobs SET location='Bangalore Urban', key='acme|ai engineer|bangalore urban'"
            " WHERE id=?", (b.job_id,),
        )  # fmt: skip
        db.conn.execute(
            "UPDATE job_links SET norm_url='https://boards.greenhouse.io/acme/jobs/1'"
            " WHERE job_id=?", (b.job_id,),
        )  # fmt: skip
    app = db.create_application(b.job_id)
    with db.conn:
        db.conn.execute(
            "UPDATE applications SET norm_title='old' WHERE id=?", (app.application_id,)
        )
    return a.job_id, b.job_id


def test_recheck_merges_colliding_rows_and_keeps_both_links(db: Database) -> None:
    a, b = stale_pair(db)
    summary = db.recheck()
    assert summary.merged == 1
    gh = "https://boards.greenhouse.io/acme/jobs/1?gh_jid=7"
    assert [r["url"] for r in db.links(a)] == [HIRIST["url"], gh]
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert db.get_job(a)["description"] == "Build RAG pipelines."
    assert {e["job_id"] for e in db.events(a)} == {a} and len(db.events(a)) >= 2
    app = db.conn.execute("SELECT job_id, norm_title FROM applications").fetchone()
    assert tuple(app) == (a, "ai engineer")
    norm = db.conn.execute("SELECT norm_url FROM job_links WHERE url=?", (gh,)).fetchone()[0]
    assert norm == normalize_url(gh)
    # Seen again after the migration, the old row is found, not copied.
    assert db.upsert_posting(posting(url=gh, location="Bangalore Urban")).outcome == "duplicate"
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_recheck_reruns_rules_on_found_and_rule_dropped_rows(db: Database) -> None:
    unpaid = db.upsert_posting(posting(description="Unpaid leave is available."))
    senior = db.upsert_posting(posting(url="https://x.example/2", company="Beta"))
    owner = db.upsert_posting(posting(url="https://x.example/3", company="Gamma"))
    closed = db.upsert_posting(posting(url="https://x.example/4", company="Delta"))
    drop(db, unpaid.job_id, "unpaid")
    drop(db, closed.job_id, "closed")
    with db.conn:
        db.conn.execute("UPDATE jobs SET title='Senior AI Engineer' WHERE id=?", (senior.job_id,))
    db.set_status(owner.job_id, "dropped", "owner")
    summary = db.recheck()
    assert (summary.undropped, summary.dropped, summary.merged) == (1, 1, 0)
    status = lambda i: db.get_job(i)["status"]  # noqa: E731
    assert status(unpaid.job_id) == "found"
    assert (status(senior.job_id), db.get_job(senior.job_id)["drop_reason"]) == (
        "dropped", "too_senior",
    )  # fmt: skip
    assert status(owner.job_id) == status(closed.job_id) == "dropped"


def test_recheck_twice_changes_nothing_the_second_time(db: Database) -> None:
    stale_pair(db)
    old = db.upsert_posting(posting(url="https://x.example/9", company="Zeta",
                                    description="Unpaid leave."))  # fmt: skip
    drop(db, old.job_id, "unpaid")
    first = db.recheck()
    assert first.merged == 1 and first.undropped == 1

    def snapshot() -> list[list[tuple[object, ...]]]:
        return [
            [tuple(r) for r in db.conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()]
            for t in ("jobs", "job_links", "applications", "events")
        ]

    before = snapshot()
    second = db.recheck()
    assert (second.undropped, second.dropped, second.merged) == (0, 0, 0)
    assert snapshot() == before
