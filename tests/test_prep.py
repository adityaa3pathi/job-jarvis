from __future__ import annotations

from typing import Any

from jobagent.config import Settings
from jobagent.db import Database
from jobagent.llm import StubLLM
from jobagent.models import Posting
from jobagent.prep import make_prep, quote_found
from tests.test_web import PASSWORD, client_for, done

PROFILE = (
    "I built exactly-once webhook processing for Razorpay with HMAC checks.\n"
    "I wrote a reconciler that refunds abandoned checkouts every 5 minutes."
)
JD = "Backend Engineer. Node.js, PostgreSQL, payments. 1-3 years."
PREP = {
    "pitch": ["I build payment backends."],
    "questions": [
        {"kind": "about_you", "question": "Tell me about webhooks.", "answer": ["Dedupe by id."],
         "based_on": "I built exactly-once webhook processing for Razorpay"},
        {"kind": "behavioral", "question": "A hard bug?", "answer": ["Kubernetes outage."],
         "based_on": "I ran Kubernetes clusters for a bank"},
        {"kind": "technical", "question": "What is an index?", "answer": ["A sorted lookup."],
         "based_on": ""},
    ],
    "gaps": [{"topic": "Kafka", "say": "Not used yet; I know queues from cron retries."}],
    "ask_them": ["What would I own first?"],
}  # fmt: skip


def responder(system: str, user: str) -> Any:
    return PREP if "interview" in system else {}


def test_a_quote_must_be_in_one_profile_line() -> None:
    assert quote_found("exactly-once webhook processing for Razorpay", PROFILE)
    assert not quote_found("I ran Kubernetes clusters for a bank", PROFILE)
    assert not quote_found("Razorpay", PROFILE)  # too short to prove anything


def test_answers_resting_on_missing_facts_are_flagged() -> None:
    prep = make_prep(title="Backend", company="Acme", jd=JD, profile=PROFILE,
                     llm=StubLLM(responder))  # fmt: skip
    grounded = [q.grounded for q in prep.questions]
    assert grounded == [True, False, True]  # technical answers need no quote


def test_prep_is_made_in_the_background_and_kept(settings: Settings) -> None:
    db = Database(settings.db_path)
    job = db.upsert_posting(
        Posting(source="hirist", url="https://www.hirist.tech/j/1", company="Acme",
                title="Backend Engineer", location="Bengaluru", description=JD)
    ).job_id  # fmt: skip
    db.close()
    llm = StubLLM(responder)
    web = client_for(settings, llm=llm)
    assert web.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert web.get(f"/api/prep/{job}").json()["prep"] is None
    assert web.post(f"/api/prep/{job}").status_code == 400  # no career summary yet
    assert web.put("/api/profile/summary", json={"summary": PROFILE}).status_code == 200
    state = done(web, web.post(f"/api/prep/{job}"))
    assert state["status"] == "done", state
    body = web.get(f"/api/prep/{job}").json()
    assert body["job"]["title"] == "Backend Engineer"
    assert body["job"]["url"] == "https://www.hirist.tech/j/1"
    assert [q["grounded"] for q in body["prep"]["questions"]] == [True, False, True]
    assert "interview_prep" in llm.steps
    assert web.post("/api/prep/999").status_code == 404
