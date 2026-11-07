"""Outreach: contact finding, facts, drafts, the store, Gmail and the web flow. Nothing real is
called: Hunter, Gmail, DNS and company sites all answer from fakes."""

from __future__ import annotations

import base64
import html
import json
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.db import Database
from jobagent.digest import send_followups
from jobagent.discovery.base import FetchResult
from jobagent.llm import FailingLLM, StubLLM
from jobagent.models import Posting
from jobagent.outreach.apollo import Apollo
from jobagent.outreach.draft import (
    FILL_PROOF,
    FILL_WHY,
    SenderProfile,
    draft_for,
    has_early_proof,
    linkedin_draft,
)
from jobagent.outreach.emails import emails_in, guesses, is_role_inbox
from jobagent.outreach.facts import Fact, Page, company_pages, grounded, pick_facts
from jobagent.outreach.finder import find_contacts, guess_domain, yc_slug
from jobagent.outreach.gmail import Gmail, GmailConfig, GmailError, Sent, build_message
from jobagent.outreach.hunter import Hunter
from jobagent.outreach.lookup import LookupError
from jobagent.outreach.network import Network, NetworkFileError, parse_connections
from jobagent.outreach.store import OutreachLimit, OutreachStore
from jobagent.web.store import Store
from tests.test_discovery import FakeFetcher
from tests.test_web import PASSWORD, client_for, done

DESC = (
    "Acme builds payment APIs for clinics. We just launched Acme Pay for Indian hospitals. "
    "You will build FastAPI services and RAG agents. Questions? Write to priya@acme.in or "
    "jobs@acme.in. See https://acme.in/careers."
)
ME = SenderProfile(
    name="Aditya",
    proof="I built https://docnow.in alone; its AI agent scores 92-96%.",
    links=["https://github.com/aditya"],
)
SITE = (
    "<h1>Acme</h1><p>Acme Pay now settles clinic payments in under two seconds across 400 Indian"
    " hospitals. Our engineering team rebuilt refunds on an idempotent ledger this quarter, and"
    " we publish weekly changelog notes about the payment rails and the clinic dashboard.</p>"
)
PAGE = Page("https://acme.in", "Acme Pay now settles clinic payments in under two seconds")
MINE = "Hi Neha,\n\nI built https://docnow.in alone, in my own words here.\n\nAditya"


def job_id(db: Database, company: str = "Acme", url: str = "https://jobs.lever.co/acme/1") -> int:
    posting = Posting(source="lever", url=url, company=company, title="Backend Engineer",
                      location="Bengaluru, India", description=DESC)  # fmt: skip
    return db.upsert_posting(posting).job_id


def sites(pages: dict[str, str]) -> Any:
    fetcher = FakeFetcher({u: FetchResult(200, t, u) for u, t in pages.items()})
    return lambda domain: fetcher


# --- emails and finding people ---


def test_guesses_put_first_name_first_and_skip_without_domain() -> None:
    assert guesses("Priya Sharma", "acme.in") == [
        "priya@acme.in", "priya.sharma@acme.in", "psharma@acme.in", "priyasharma@acme.in",
    ]  # fmt: skip
    assert guesses("Priya", "acme.in") == ["priya@acme.in"]
    assert guesses("Priya Sharma", "") == []


def test_emails_in_keeps_only_the_company_domain_and_drops_junk() -> None:
    text = "a@acme.in, b@eu.acme.in, x@other.com, logo@2x.png, c@acme.in"
    assert emails_in(text, "acme.in") == ["a@acme.in", "b@eu.acme.in", "c@acme.in"]
    assert is_role_inbox("jobs@acme.in") and not is_role_inbox("priya@acme.in")


def test_domain_comes_from_a_posting_link_naming_the_company() -> None:
    assert guess_domain("Acme", [DESC]) == "acme.in"
    assert guess_domain("Acme", ["apply at https://boards.greenhouse.io/acme"]) == ""


def test_finder_ranks_named_people_before_shared_inboxes() -> None:
    got = find_contacts(
        company="Acme", description=DESC, urls=[], board_fetcher=FakeFetcher({}),
        site_fetcher=sites({"https://acme.in/contact": "mail cto@acme.in"}),
        has_mx=lambda d: True,
    )  # fmt: skip
    emails = [c.email for c in got.candidates]
    assert got.domain == "acme.in"
    assert emails[-1] == "jobs@acme.in"  # a shared inbox comes last
    assert set(emails) == {"priya@acme.in", "jobs@acme.in", "cto@acme.in"}


def yc_page(founders: list[dict[str, str]], website: str = "https://acme.in") -> str:
    data = {"props": {"company": {"website": website, "founders": founders}}}
    return f'<div data-page="{html.escape(json.dumps(data))}"></div>'


def test_yc_founders_get_guessed_emails_only_when_the_domain_takes_mail() -> None:
    url = "https://www.ycombinator.com/companies/acme/jobs/abc-backend"
    assert yc_slug([url]) == "acme"
    board = FakeFetcher({"https://www.ycombinator.com/companies/acme": FetchResult(
        200, yc_page([{"full_name": "Ravi Kumar", "title": "CTO"}]), "x")})  # fmt: skip
    kwargs: dict[str, Any] = dict(
        company="Acme", description="", urls=[url], board_fetcher=board, site_fetcher=sites({})
    )
    with_mx = find_contacts(**kwargs, has_mx=lambda d: True)
    ravi = with_mx.candidates[0]
    assert (ravi.name, ravi.email, ravi.email_source) == ("Ravi Kumar", "ravi@acme.in", "guess")
    no_mx = find_contacts(**kwargs, has_mx=lambda d: False)
    assert no_mx.candidates[0].email == ""
    assert any("no mail server" in n for n in no_mx.notes)


def hunter_transport(status: int = 200, seen: list[httpx.Request] | None = None) -> Any:
    body = {"data": {"domain": "acme.in", "emails": [
        {"value": "Neha@acme.in", "first_name": "Neha", "last_name": "Rao",
         "position": "Engineering Manager", "confidence": 94, "verification": {"status": "valid"}},
        {"value": "x@acme.in", "first_name": None, "last_name": None},
    ]}}  # fmt: skip

    def handle(request: httpx.Request) -> httpx.Response:
        (seen if seen is not None else []).append(request)
        return httpx.Response(status, json=body if status == 200 else {"errors": []})

    return httpx.MockTransport(handle)


def test_hunter_parses_people_and_keeps_the_key_out_of_the_url() -> None:
    seen: list[httpx.Request] = []
    hunter = Hunter("secret-key", transport=hunter_transport(seen=seen))
    got = hunter.people_at(domain="acme.in")
    assert [(p.name, p.email, p.note) for p in got.people] == [
        ("Neha Rao", "neha@acme.in", "Hunter verified")
    ]
    assert "secret-key" not in str(seen[0].url)
    assert seen[0].headers["X-API-KEY"] == "secret-key"


@pytest.mark.parametrize(("status", "words"), [(401, "rejected"), (429, "limit"), (500, "500")])
def test_hunter_errors_are_plain(status: int, words: str) -> None:
    with pytest.raises(LookupError, match=words):
        Hunter("k", transport=hunter_transport(status)).people_at(domain="", company="Acme")


def test_a_hunter_failure_is_only_a_note() -> None:
    got = find_contacts(
        company="Acme", description=DESC, urls=[], board_fetcher=FakeFetcher({}),
        site_fetcher=sites({}), has_mx=lambda d: True,
        lookups=[Hunter("k", transport=hunter_transport(429))],
    )  # fmt: skip
    assert got.candidates and any("Hunter limit" in n for n in got.notes)


class ApolloFake:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "x"})
        body = json.loads(request.content)
        if request.url.path.endswith("/mixed_people/api_search"):
            return httpx.Response(200, json={"people": [
                {"id": "p1", "first_name": "Sam", "title": "VP Sales", "has_email": True},
                {"id": "p2", "first_name": "Neha", "title": "Head of Engineering",
                 "has_email": True},
                {"id": "p3", "first_name": "Raj", "title": "CTO", "has_email": False},
            ]})  # fmt: skip
        people = {
            "p1": {"name": "Sam Lee", "title": "VP Sales", "email": "sam@acme.in",
                   "email_status": "verified"},
            "p2": {"name": "Neha Rao", "title": "Head of Engineering", "email": "Neha@acme.in",
                   "email_status": "verified", "linkedin_url": "https://linkedin.com/in/neha"},
        }  # fmt: skip
        return httpx.Response(200, json={"person": people[body["id"]]})

    def client(self, reveal: int = 3) -> Apollo:
        return Apollo("apollo-key", transport=httpx.MockTransport(self.handle), reveal=reveal)


def test_apollo_reveals_engineering_deciders_first_and_skips_people_without_email() -> None:
    fake = ApolloFake()
    got = fake.client(reveal=1).people_at(domain="acme.in")
    assert [(p.name, p.email, p.note) for p in got.people] == [
        ("Neha Rao", "neha@acme.in", "Apollo verified")
    ]
    revealed = [json.loads(r.content)["id"] for r in fake.requests if "match" in r.url.path]
    assert revealed == ["p2"]  # one credit, spent on the engineering head
    assert all(r.headers["x-api-key"] == "apollo-key" for r in fake.requests)
    assert "apollo-key" not in str(fake.requests[0].url)


def test_apollo_needs_a_domain_and_explains_plan_errors() -> None:
    with pytest.raises(LookupError, match="company website"):
        ApolloFake().client().people_at(domain="")
    with pytest.raises(LookupError, match="work email"):
        ApolloFake(403).client().people_at(domain="acme.in")


def test_finder_merges_apollo_people_with_free_sources() -> None:
    got = find_contacts(
        company="Acme", description=DESC, urls=[], board_fetcher=FakeFetcher({}),
        site_fetcher=sites({}), has_mx=lambda d: True, lookups=[ApolloFake().client()],
    )  # fmt: skip
    by_email = {c.email: c for c in got.candidates}
    assert by_email["neha@acme.in"].source == "apollo"
    assert by_email["neha@acme.in"].linkedin_url == "https://linkedin.com/in/neha"


# --- facts and drafts ---


def test_grounding_keeps_only_quotes_from_company_pages_not_the_job_post() -> None:
    real = Fact(fact="Fast settlement", quote="settles clinic payments in under two",
                page="https://acme.in")  # fmt: skip
    made_up = Fact(fact="Raised $50M", quote="Acme raised fifty million dollars last",
                   page="https://acme.in")  # fmt: skip
    from_job = Fact(fact="Launched Acme Pay", quote="We just launched Acme Pay for",
                    page="https://acme.in")  # fmt: skip
    page = Page("https://acme.in", PAGE.text + " We just launched Acme Pay for Indian hospitals.")
    assert grounded([made_up, from_job, real], DESC, [page]) == [real]


def test_company_pages_reads_the_companys_own_pages_and_skips_missing_ones() -> None:
    pages = company_pages(sites({"https://acme.in": SITE, "https://acme.in/changelog": SITE,
                                 "https://blog.acme.in": SITE + " Blog."}), "acme.in")  # fmt: skip
    assert [p.url for p in pages] == [
        "https://acme.in",
        "https://blog.acme.in",
    ]  # duplicate dropped
    assert "Acme Pay now settles" in pages[0].text and "<p>" not in pages[0].text


def test_pick_facts_needs_company_pages_and_survives_a_failing_model() -> None:
    assert pick_facts(DESC, [], StubLLM(lambda s, u: {})) == []
    assert pick_facts(DESC, [PAGE], FailingLLM()) == []


def test_pick_facts_caches_the_checked_result(settings: Settings) -> None:
    llm = StubLLM(lambda s, u: {"facts": [
        {"fact": "Fast settlement", "quote": "settles clinic payments in under two seconds",
         "page": "https://acme.in"}]})  # fmt: skip
    st = Store(settings.data_dir / "x.sqlite3")
    first = pick_facts(DESC, [PAGE], llm, cache=st)
    second = pick_facts(DESC, [PAGE], llm, cache=st)
    assert [f.fact for f in first] == [f.fact for f in second] == ["Fast settlement"]
    assert len(llm.calls) == 1
    assert "priya@acme.in" not in llm.calls[0][1]  # emails never reach the AI


def test_drafts_open_with_the_proof_line() -> None:
    d = draft_for("email", 1, name="Neha", title="Backend Engineer", company="Acme", facts=[],
                  me=ME)  # fmt: skip
    assert d.body.split("\n\n")[1] == ME.proof and has_early_proof(d.body)
    bare = draft_for("email", 1, name="Neha", title="Backend Engineer", company="Acme",
                     facts=[], me=SenderProfile())  # fmt: skip
    assert FILL_PROOF in bare.body and FILL_WHY in bare.body
    assert bare.subject == "Backend Engineer role at Acme"
    assert not has_early_proof("Hi,\n\n" + "words " * 100 + "\nhttps://docnow.in")


def test_linkedin_note_stays_under_300_characters_and_keeps_the_proof() -> None:
    long_fact = Fact(fact="x" * 280, quote="q q q q")
    note = linkedin_draft(1, name="Neha Rao", title="Backend Engineer", company="Acme",
                          facts=[long_fact], me=ME)  # fmt: skip
    assert len(note.body) <= 300 and note.body.startswith("Hi Neha,")
    assert "docnow.in" in note.body and "xxxx" not in note.body


# --- warm paths ---

EXPORT = (
    b'Notes:\n"When exporting your connection data, you may notice..."\n\n'
    b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    b"Ravi,Kumar,https://www.linkedin.com/in/ravi,,Acme Inc,Staff Engineer,01 Oct 2026\n"
    b"Meera,Shah,https://www.linkedin.com/in/meera,meera@x.com,Other,EM,02 Oct 2026\n"
    b"No,Company,https://www.linkedin.com/in/no,,,,03 Oct 2026\n"
)


def test_linkedin_export_is_parsed_past_its_notes_header() -> None:
    rows = parse_connections(EXPORT)
    assert [(r["name"], r["company"]) for r in rows] == [("Ravi Kumar", "Acme Inc"),
                                                         ("Meera Shah", "Other")]  # fmt: skip
    with pytest.raises(NetworkFileError):
        parse_connections(b"name,company\nx,y\n")


def test_known_people_match_job_companies(settings: Settings) -> None:
    db = Database(settings.db_path)
    jid = job_id(db)
    net = Network(db.conn)
    assert net.replace(parse_connections(EXPORT)) == 2
    assert [p["name"] for p in net.at("ACME")] == ["Ravi Kumar"]
    match = net.matches()[0]
    assert match["company"] == "Acme" and match["jobs"][0]["id"] == jid
    assert net.replace([]) == 0 and net.summary()["people"] == 0  # a new export replaces the old


# --- the store ---


def test_two_people_per_company_and_the_same_email_is_one_contact(settings: Settings) -> None:
    db = Database(settings.data_dir / "j.sqlite3")
    out = OutreachStore(db.conn)
    kw: dict[str, Any] = dict(title="", email_source="manual", linkedin_url="", limit=2)
    a = out.add_contact("Acme", name="A", email="a@acme.in", **kw)
    assert out.add_contact("ACME Inc", name="A again", email="a@acme.in", **kw) == a
    out.add_contact("Acme", name="B", email="b@acme.in", **kw)
    with pytest.raises(OutreachLimit):
        out.add_contact("Acme", name="C", email="c@acme.in", **kw)


def test_old_contacts_are_blanked_but_counts_stay(settings: Settings) -> None:
    db = Database(settings.data_dir / "j.sqlite3")
    out = OutreachStore(db.conn)
    jid = job_id(db)
    cid = out.add_contact("Acme", name="A", title="", email="a@acme.in", email_source="manual",
                          linkedin_url="", limit=2)  # fmt: skip
    oid = out.start(jid, cid, "email", [], draft_for("email", 1, name="A", title="t",
                                                      company="Acme", facts=[], me=ME))  # fmt: skip
    out.record_send(out.get(oid) or {}, next_draft=None, next_due=None)
    later = datetime.now(UTC) + timedelta(days=91)
    assert out.clear_old(90, now=later) == 1
    row = db.conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    assert (row["name"], row["email"]) == ("", "")
    assert out.stats(7)["sent"] == 1


# --- Gmail ---


class GoogleFake:
    def __init__(self, thread: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.thread = thread or []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if path.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
        if "/messages/" in path:
            return httpx.Response(200, json={"payload": {"headers": [
                {"name": "Message-ID", "value": "<abc@mail.gmail.com>"}]}})  # fmt: skip
        if "/threads/" in path:
            return httpx.Response(200, json={"messages": self.thread})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    def client(self) -> Gmail:
        cfg = GmailConfig("cid", "csecret", "refresh-secret")
        return Gmail(cfg, transport=httpx.MockTransport(self.handle))


def test_gmail_sends_plain_text_in_the_thread_and_reuses_its_token() -> None:
    fake = GoogleFake()
    gmail = fake.client()
    msg = build_message("neha@acme.in", "Re: hi", "body text", in_reply_to="<prev@x>")
    sent = gmail.send(msg, "t1")
    gmail.send(build_message("neha@acme.in", "hi", "again"))
    assert sent == Sent("m1", "t1", "<abc@mail.gmail.com>")
    assert sum(r.url.path.endswith("/token") for r in fake.requests) == 1
    first_send = json.loads(next(r for r in fake.requests if r.url.path.endswith("/send")).content)
    assert first_send["threadId"] == "t1"
    mime = message_from_bytes(base64.urlsafe_b64decode(first_send["raw"]))
    assert mime["In-Reply-To"] == "<prev@x>" and mime.get_content_type() == "text/plain"


def msg(labels: list[str], sender: str) -> dict[str, Any]:
    return {"labelIds": labels, "payload": {"headers": [{"name": "From", "value": sender}]}}


@pytest.mark.parametrize(
    ("thread", "state"),
    [
        ([msg(["SENT"], "me@gmail.com")], "none"),
        ([msg(["SENT"], "me"), msg(["INBOX"], "Neha <neha@acme.in>")], "replied"),
        ([msg(["SENT"], "me"), msg(["INBOX"], "Mail Delivery <mailer-daemon@google>")], "bounced"),
    ],
)
def test_gmail_reads_only_headers_to_spot_replies(thread: list[Any], state: str) -> None:
    assert GoogleFake(thread).client().reply_state("t1") == state


def test_gmail_errors_never_hold_the_token() -> None:
    gmail = Gmail(GmailConfig("cid", "csecret", "refresh-secret"), transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"message": "invalid_grant"}})))  # fmt: skip
    with pytest.raises(GmailError) as err:
        gmail.reply_state("t1")
    assert "refresh-secret" not in str(err.value) and "invalid_grant" in str(err.value)


# --- the web flow ---


class FakeGmail:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, str]] = []
        self.state = "none"

    def send(self, message: Any, thread_id: str = "") -> Sent:
        self.sent.append((message, thread_id))
        return Sent(f"m{len(self.sent)}", "t1", f"<id{len(self.sent)}@x>")

    def reply_state(self, thread_id: str) -> str:
        return self.state


def facts_llm(system: str, user: str) -> Any:
    if "personal note" in system:
        return {"facts": [{"fact": "Fast settlement", "page": "https://acme.in",
                           "quote": "settles clinic payments in under two seconds"}]}  # fmt: skip
    return {}


@pytest.fixture
def flow(settings: Settings) -> tuple[TestClient, FakeGmail, int]:
    settings.outreach.first_emails_per_day = 1
    gmail = FakeGmail()
    site = sites({"https://acme.in": SITE})
    c = client_for(settings, llm=StubLLM(facts_llm), gmail=lambda: gmail, site_fetcher=site,
                   has_mx=lambda d: True)  # fmt: skip
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    db = Database(settings.db_path)
    return c, gmail, job_id(db)


def start_thread(
    c: TestClient, jid: int, channel: str, email: str = "neha@acme.in", name: str = "Neha Rao"
) -> int:
    contact = c.post(f"/api/jobs/{jid}/contacts", json={
        "name": name, "title": "EM", "email": email, "email_source": "manual"})  # fmt: skip
    assert contact.status_code == 200, contact.text
    cid = contact.json()["id"]
    state = done(
        c, c.post(f"/api/contacts/{cid}/outreach", json={"job_id": jid, "channel": channel})
    )
    assert state["status"] == "done", state
    return int(state["result"]["outreach_id"])


def edit(c: TestClient, oid: int, body: str, subject: str = "Backend role") -> None:
    assert c.put(f"/api/outreach/{oid}", json={"subject": subject, "body": body}).status_code == 200


def test_outreach_routes_need_a_login(settings: Settings) -> None:
    c = client_for(settings)
    assert c.get("/api/outreach").status_code == 401
    assert c.post("/api/outreach/1/send").status_code == 401


def test_find_returns_people_without_saving_them(flow: Any) -> None:
    c, _, jid = flow
    state = done(c, c.post(f"/api/jobs/{jid}/contacts/find", json={}))
    assert state["status"] == "done"
    assert "priya@acme.in" in [x["email"] for x in state["result"]["candidates"]]
    assert c.get(f"/api/jobs/{jid}/outreach").json()["contacts"] == []


@pytest.mark.parametrize(
    ("flag", "env"), [("use_hunter", "HUNTER_API_KEY"), ("use_apollo", "APOLLO_API_KEY")]
)
def test_paid_lookups_need_a_key(flow: Any, flag: str, env: str) -> None:
    c, _, jid = flow
    res = c.post(f"/api/jobs/{jid}/contacts/find", json={flag: True})
    assert res.status_code == 400 and env in res.text


def test_email_flow_needs_an_edit_then_sends_and_schedules_follow_ups(flow: Any) -> None:
    c, gmail, jid = flow
    oid = start_thread(c, jid, "email")
    view = c.get(f"/api/jobs/{jid}/outreach").json()["threads"][0]
    assert "Fast settlement" in view["body"] and not view["edited"]
    assert view["facts"][0]["page"] == "https://acme.in"
    assert c.post(f"/api/outreach/{oid}/send").status_code == 409  # not edited yet
    edit(c, oid, MINE)
    res = c.post(f"/api/outreach/{oid}/send")
    assert res.status_code == 200, res.text
    message, thread = gmail.sent[0]
    assert (message["To"], thread) == ("neha@acme.in", "")
    after = c.get(f"/api/jobs/{jid}/outreach").json()["threads"][0]
    first = datetime.fromisoformat(after["first_sent_at"]).astimezone(UTC)
    assert after["step"] == 1 and after["state"] == "waiting"
    assert after["next_due"] >= (first + timedelta(days=3)).date().isoformat()
    assert "Bringing this back" in after["body"] and not after["edited"]
    edit(c, oid, "Hi Neha, a quick nudge on this. Aditya", subject=after["subject"])
    assert c.post(f"/api/outreach/{oid}/send").status_code == 200
    follow, thread = gmail.sent[1]
    assert thread == "t1" and follow["Subject"].startswith("Re: ")
    assert follow["In-Reply-To"] == "<id1@x>"


def test_a_follow_up_is_refused_once_they_replied(flow: Any) -> None:
    c, gmail, jid = flow
    oid = start_thread(c, jid, "email")
    edit(c, oid, MINE)
    assert c.post(f"/api/outreach/{oid}/send").status_code == 200
    gmail.state = "replied"
    edit(c, oid, "Hi Neha, nudge in my words. Aditya")
    res = c.post(f"/api/outreach/{oid}/send")
    assert res.status_code == 409 and "replied" in res.text
    assert len(gmail.sent) == 1
    assert c.get("/api/outreach").json()["items"][0]["state"] == "replied"


def test_the_daily_cap_on_first_emails(flow: Any, settings: Settings) -> None:
    c, gmail, jid = flow
    first = start_thread(c, jid, "email")
    edit(c, first, MINE)
    assert c.post(f"/api/outreach/{first}/send").status_code == 200
    second = start_thread(c, jid, "email", email="arjun@acme.in", name="Arjun Das")
    edit(c, second, MINE.replace("Neha", "Arjun"))
    res = c.post(f"/api/outreach/{second}/send")
    assert res.status_code == 429 and len(gmail.sent) == 1


def test_unfilled_template_gaps_block_sending(flow: Any) -> None:
    c, _, jid = flow
    oid = start_thread(c, jid, "email")
    edit(c, oid, f"Hi Neha,\n\n{FILL_PROOF}\n\nAditya")
    res = c.post(f"/api/outreach/{oid}/send")
    assert res.status_code == 409 and "fill in" in res.text
    edit(c, oid, "Hi Neha,\n\nMy own words, no link to my work.\n\nAditya")
    res = c.post(f"/api/outreach/{oid}/send")
    assert res.status_code == 409 and "link to something you built" in res.text


def test_linkedin_flow_is_marked_by_hand_and_waits_for_acceptance(flow: Any) -> None:
    c, gmail, jid = flow
    oid = start_thread(c, jid, "linkedin")
    assert c.put(f"/api/outreach/{oid}", json={"body": "x" * 301}).status_code == 400
    edit(c, oid, "Hi Neha, applying for the backend role. I built docnow.in alone.", subject="")
    assert c.post(f"/api/outreach/{oid}/send").status_code == 200
    edit(c, oid, "Hi Neha, thanks for connecting. My own message.", subject="")
    assert c.post(f"/api/outreach/{oid}/send").status_code == 409  # not accepted yet
    assert c.post(f"/api/outreach/{oid}/state", json={"state": "accepted"}).status_code == 200
    assert c.post(f"/api/outreach/{oid}/send").status_code == 200
    assert gmail.sent == []  # LinkedIn steps are never sent by the app
    row = c.get("/api/outreach").json()["items"][0]
    assert (row["step"], row["can_send"]) == (2, False)


def test_check_replies_marks_replied_threads(flow: Any) -> None:
    c, gmail, jid = flow
    oid = start_thread(c, jid, "email")
    edit(c, oid, MINE)
    c.post(f"/api/outreach/{oid}/send")
    gmail.state = "replied"
    state = done(c, c.post("/api/outreach/check-replies"))
    assert state["result"]["replied"] == 1


def test_no_gmail_means_no_email_sending(settings: Settings) -> None:
    c = client_for(settings, llm=StubLLM(facts_llm), site_fetcher=sites({"https://acme.in": SITE}),
                   has_mx=lambda d: True)  # fmt: skip
    c.post("/api/login", json={"password": PASSWORD})
    jid = job_id(Database(settings.db_path))
    oid = start_thread(c, jid, "email")
    edit(c, oid, MINE)
    res = c.post(f"/api/outreach/{oid}/send")
    assert res.status_code == 400 and "Gmail is not connected" in res.text
    assert c.get("/api/outreach").json()["ready"] == {
        "gmail": False,
        "hunter": False,
        "apollo": False,
    }


def test_network_import_shows_known_people_on_the_job(flow: Any) -> None:
    c, _, jid = flow
    bad = c.post("/api/network/import", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert bad.status_code == 400
    res = c.post("/api/network/import", files={"file": ("Connections.csv", EXPORT, "text/csv")})
    assert res.json() == {"imported": 2}
    known = c.get(f"/api/jobs/{jid}/outreach").json()["known"]
    assert [k["name"] for k in known] == ["Ravi Kumar"]
    net = c.get("/api/network").json()
    assert (net["people"], net["matches"][0]["company"]) == (2, "Acme")
    assert c.delete("/api/network").json() == {"removed": 2}


# --- Telegram reminder ---


class Collect:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send(self, text: str) -> None:
        self.texts.append(text)


def test_follow_up_reminder_goes_once_a_day(settings: Settings) -> None:
    db = Database(settings.db_path)
    out = OutreachStore(db.conn)
    cid = out.add_contact("Acme", name="Neha Rao", title="", email="n@acme.in",
                          email_source="manual", linkedin_url="", limit=2)  # fmt: skip
    oid = out.start(
        job_id(db),
        cid,
        "email",
        [],
        draft_for("email", 1, name="Neha", title="t", company="Acme", facts=[], me=ME),
    )
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    out.record_send(out.get(oid) or {}, next_draft=None, next_due=yesterday)
    sender = Collect()
    assert send_followups(db, sender)["sent"] == 1
    assert "Neha Rao" in sender.texts[0] and "follow-up" in sender.texts[0]
    assert send_followups(db, sender)["skipped"] == "already sent today"
