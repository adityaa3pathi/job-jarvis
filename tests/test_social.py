"""Social sources (Telegram, Bluesky, Mastodon, Reddit) and their shared filter. Every sample
here is made up, so no real person's post or name is stored in the repo."""

from __future__ import annotations

import json
from datetime import date, timedelta

from jobagent.discovery import BOARD_ADAPTERS, discover_all
from jobagent.discovery.base import BoardSearch, FetchResult
from jobagent.discovery.social import company_of, drop_reason, title_of
from jobagent.policy import Policy
from tests.test_discovery import FakeFetcher

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=60)).isoformat()


def run(board: str, term: str, body: str, policy: Policy) -> list:  # type: ignore[type-arg]
    search = BoardSearch(board=board, term=term)  # type: ignore[arg-type]
    url = BOARD_ADAPTERS[board].url_for(search)
    res = discover_all([], FakeFetcher({url: FetchResult(200, body, url)}), policy, [search])
    assert res.unreachable == [] and res.skipped_by_policy == []
    return res.postings


def test_filter_keeps_tech_hiring_and_drops_scams_self_promo_and_non_tech() -> None:
    assert drop_reason("We're hiring a Python backend engineer, remote India.") is None
    assert drop_reason("Part time work from home, earn ₹5000 daily, WhatsApp me now") == (
        "scam_pattern"
    )
    assert drop_reason("Registration fee ₹999. Hiring Python developers") == "scam_pattern"
    assert drop_reason("[For Hire] Full stack developer, 5 years, DM me") == "for_hire"
    assert drop_reason("If you're looking for a developer to build your app, DM me") == ("for_hire")
    assert drop_reason("We're hiring a barista for our cafe") == "not_tech"
    assert drop_reason("Shipped a new feature in our React app today") == "not_hiring"


def test_titles_and_companies_come_from_structured_lines() -> None:
    post = "💼 JOB OPPORTUNITY 🚀\nSenior Full Stack Developer\n🏢 Company: Acme Labs\nApply now"
    assert (title_of(post), company_of(post, "x")) == ("Senior Full Stack Developer", "Acme Labs")
    listing = "Company Acme India\nRoles Backend Developer & other\nQualification B.Tech"
    assert (title_of(listing), company_of(listing, "x")) == ("Backend Developer & other",
                                                             "Acme India")  # fmt: skip
    assert title_of("GitLab is hiring Backend Engineer, Remote") == "Backend Engineer, Remote"
    assert company_of("GitLab is hiring Backend Engineer, Remote", "x") == "GitLab"


def test_a_location_line_lets_the_india_rule_work(policy: Policy) -> None:
    page = "<div class='tgme_channel_info'></div>" + tg_message(
        "jobsdemo/9", "We're hiring a Python developer<br/>Location: Bengaluru, India", TODAY
    )
    (job,) = run("telegram", "jobsdemo", page, policy)
    assert job.location == "Bengaluru, India"


def tg_message(post: str, text: str, when: str) -> str:
    return (
        f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div data-post="{post}">'
        f'<div class="tgme_widget_message_text js-message_text">{text}</div>'
        f'<time datetime="{when}T10:00:00+00:00">x</time></div></div>'
    )


def test_telegram_reads_the_channel_preview_per_message(policy: Policy) -> None:
    page = "<div class='tgme_channel_info'></div>" + "".join([
        tg_message("jobsdemo/1", "JOB OPPORTUNITY<br/>Python Backend Developer<br/>"
                   "🏢 Company: Acme<br/>Apply now", TODAY),
        '<div class="tgme_widget_message_wrap"><div data-post="jobsdemo/2">photo</div></div>',
        tg_message("jobsdemo/3", "Earn ₹3000 daily from home, part time typing job", TODAY),
        tg_message("jobsdemo/4", "We're hiring a Node.js engineer", OLD),
    ])  # fmt: skip
    (job,) = run("telegram", "jobsdemo developer", page, policy)
    assert (
        BOARD_ADAPTERS["telegram"].url_for(BoardSearch(board="telegram", term="jobsdemo developer"))
        == "https://t.me/s/jobsdemo?q=developer"
    )
    assert (job.url, job.title, job.company) == (
        "https://t.me/jobsdemo/1", "Python Backend Developer", "Acme",
    )  # fmt: skip


def test_bluesky_skips_job_bots_and_keeps_people_hiring(policy: Policy) -> None:
    def post(handle: str, name: str, text: str) -> dict[str, object]:
        return {"uri": f"at://did:plc:x/app.bsky.feed.post/{handle[:3]}1",
                "author": {"handle": handle, "displayName": name},
                "record": {"text": text, "createdAt": f"{TODAY}T01:00:00Z"}}  # fmt: skip

    body = json.dumps({"posts": [
        post("asha.bsky.social", "Asha", "We're hiring a backend engineer (Python, FastAPI)."),
        post("remotejobs.bsky.social", "Remote Jobs", "We're hiring a backend engineer!"),
    ]})  # fmt: skip
    (job,) = run("bluesky", "hiring backend engineer", body, policy)
    assert job.url == "https://bsky.app/profile/asha.bsky.social/post/ash1"
    assert job.company == "Asha"


def test_mastodon_reads_the_hiring_tag_feed(policy: Policy) -> None:
    rss = (
        "<rss><channel><item><link>https://fosstodon.example/@devjobs/1</link>"
        f"<pubDate>{date.today().strftime('%a, %d %b %Y')} 08:00:00 +0000</pubDate>"
        "<description>&lt;p&gt;Acme is hiring Senior Python Engineer, remote&lt;/p&gt;"
        "</description></item></channel></rss>"
    )
    (job,) = run("mastodon", "fosstodon.org", rss, policy)
    assert (job.company, job.title) == ("Acme", "Senior Python Engineer, remote")


def test_reddit_reads_hiring_posts_from_search_rss(policy: Policy) -> None:
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<author><name>/u/demo</name></author>"
        '<link href="https://www.reddit.com/r/forhire/comments/abc/x/"/>'
        f"<updated>{TODAY}T09:00:00+00:00</updated>"
        "<title>[Hiring] Python developer for a FastAPI backend</title>"
        '<content type="html">&lt;p&gt;Paid, $40/hr, remote.&lt;/p&gt;</content>'
        "</entry></feed>"
    )
    (job,) = run("reddit", "forhire", atom, policy)
    assert job.title == "Python developer for a FastAPI backend"
    assert job.company == "r/forhire: /u/demo"
