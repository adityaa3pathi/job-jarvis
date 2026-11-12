from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from jobagent.config import Settings
from jobagent.db import Database
from jobagent.discovery.base import FetchResult
from jobagent.funding import (
    FEEDS,
    FundingNews,
    FundingStore,
    company_from_headline,
    parse_feed,
    read_funding,
)
from jobagent.models import Posting
from jobagent.policy import Policy
from jobagent.runs import RunTally, funding_after_run
from tests.test_discovery import FakeFetcher
from tests.test_web import PASSWORD, client_for


@pytest.mark.parametrize(
    ("headline", "company"),
    [
        ("Hundo Pizza raises maiden institutional round led by Titan Capital", "Hundo Pizza"),
        ("Exclusive: Olyv raises Rs 58 Cr in extended Series C round", "Olyv"),
        ("Exclusive: Dhruva Space to raise $15.5 Mn in pre-Series B; revenue surges 8X",
         "Dhruva Space"),
        ("D2C menswear brand QB MEN raises pre seed funding led by BeyondSeed", "QB MEN"),
        ("Ecommerce Fulfilment Startup Zippee Bags Funding From Haldiram&#8217;s", "Zippee"),
        ("Former Flipkart Exec Anil Goteti’s Scapia Raises $9 Mn In Seed Funding", "Scapia"),
        ("Sarvam AI Secures $41 Mn Series A", "Sarvam AI"),
        ("[Update] Greenjoules Closes Series B Round At ₹150 Cr", "Greenjoules"),
        ("Fintech infra startup OneStack raises Rs 12.5 Cr from Shepherd's Hill", "OneStack"),
    ],
)  # fmt: skip
def test_company_is_read_from_a_funding_headline(headline: str, company: str) -> None:
    assert company_from_headline(headline) == company


@pytest.mark.parametrize(
    "headline",
    [
        "Canonicus Capital raises Rs 192 Cr in first close of Rs 500 Cr special situations fund",
        "Neeman’s revenue jumps 73% to Rs 180 Cr in FY26, losses narrow 30%",
        "Sachin Tendulkar joins Recykal as strategic investor",
        "Zomato raises prices for Gold members",
        "InMobi Secures Naming Rights For Bengaluru’s Metro Station For ₹65 Cr",
        "From GIVA To EloElo Group — Indian Startups Raised $255 Mn This Week",
        "Startup Funding Plunges Nearly 80% YoY To $580 Mn In June 2023",
    ],
)
def test_headlines_that_are_not_a_startup_raise_are_skipped(headline: str) -> None:
    assert company_from_headline(headline) is None


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Olyv raises Rs 58 Cr in Series C</title><link>https://entrackr.com/a</link>
<pubDate>Tue, 10 Nov 2026 09:00:00 +0530</pubDate></item>
<item><title>HomeLane revenue stays flat</title><link>https://entrackr.com/b</link></item>
<item><title>Acme bags $2 Mn seed funding</title><link>http://entrackr.com/c</link></item>
</channel></rss>"""


def test_feed_keeps_https_raises_with_their_date() -> None:
    news = parse_feed(RSS, "entrackr")
    assert [(n.company, n.url, n.published) for n in news] == [
        ("Olyv", "https://entrackr.com/a", date(2026, 11, 10))
    ]


def test_one_broken_feed_costs_only_its_own_news(policy: Policy) -> None:
    fetcher = FakeFetcher({FEEDS["entrackr"]: FetchResult(200, RSS, FEEDS["entrackr"])})
    read = read_funding(fetcher, policy)  # type: ignore[arg-type]
    assert [n.company for n in read.news] == ["Olyv"]
    assert len(read.notes) == 1 and read.notes[0].startswith("inc42:")


def _news(company: str, url: str, days_ago: int) -> FundingNews:
    when = datetime.now(UTC).date() - timedelta(days=days_ago)
    return FundingNews(company, f"{company} raises $5 Mn", url, when, "inc42")


def test_store_dedupes_by_link_and_finds_recent_raises(settings: Settings) -> None:
    db = Database(settings.db_path)
    store = FundingStore(db.conn)
    assert store.save([_news("Olyv", "https://x/1", 3), _news("Olyv", "https://x/1", 3)]) == 1
    assert store.save([_news("Olyv", "https://x/1", 3), _news("Old Co", "https://x/2", 200)]) == 1
    found = store.funded(["Olyv Pvt Ltd", "Old Co", "Nobody"])
    assert set(found) == {"Olyv Pvt Ltd"}  # a 200-day-old raise is not "recent"
    assert found["Olyv Pvt Ltd"]["url"] == "https://x/1"
    db.close()


def test_recent_raises_show_open_jobs_and_watch_list(settings: Settings) -> None:
    db = Database(settings.db_path)
    db.upsert_posting(
        Posting(
            source="hirist",
            url="https://www.hirist.tech/j/1",
            company="Olyv",
            title="Backend Engineer",
            location="Bengaluru",
        )
    )
    store = FundingStore(db.conn)
    store.save([_news("Olyv", "https://x/1", 1), _news("Hundo Pizza", "https://x/2", 2)])
    rows = {r["company"]: r for r in store.recent(watched=["Hundo Pizza"])}
    assert rows["Olyv"]["open_jobs"] == 1 and not rows["Olyv"]["watched"]
    assert rows["Hundo Pizza"]["open_jobs"] == 0 and rows["Hundo Pizza"]["watched"]
    db.close()


def test_run_step_never_raises(settings: Settings, policy: Policy) -> None:
    class Broken:
        def get(self, url: str) -> FetchResult:
            raise OSError("offline")

    db, tally = Database(settings.db_path), RunTally()
    funding_after_run(db, Broken(), policy, tally)  # type: ignore[arg-type]
    assert len(tally.notes) == 2
    db.close()


def test_jobs_carry_a_funded_badge(settings: Settings) -> None:
    web = client_for(settings)
    assert web.post("/api/login", json={"password": PASSWORD}).status_code == 200
    db = Database(settings.db_path)
    db.upsert_posting(
        Posting(
            source="hirist",
            url="https://www.hirist.tech/j/1",
            company="Olyv",
            title="Backend Engineer",
            location="Bengaluru",
        )
    )
    FundingStore(db.conn).save([_news("Olyv", "https://x/1", 1)])
    db.close()
    item = web.get("/api/jobs").json()["items"][0]
    assert item["funded"]["url"] == "https://x/1"
    rows = web.get("/api/funding").json()
    assert rows[0]["company"] == "Olyv" and rows[0]["open_jobs"] == 1
