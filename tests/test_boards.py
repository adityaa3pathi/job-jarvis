from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from jobagent.discovery import BOARD_ADAPTERS, discover_all
from jobagent.discovery.base import BoardSearch, FetchResult, load_searches
from jobagent.discovery.fetch import KNOWN_BOARD_HOSTS
from jobagent.policy import Policy
from tests.test_discovery import FIX, FakeFetcher

REPO = Path(__file__).parents[1]


def run(board: str, term: str, fixture: str, policy: Policy) -> list:  # type: ignore[type-arg]
    search = BoardSearch(board=board, term=term)  # type: ignore[arg-type]
    url = BOARD_ADAPTERS[board].url_for(search)
    page = FetchResult(200, (FIX / "boards" / fixture).read_text(), url)
    res = discover_all([], FakeFetcher({url: page}), policy, [search])
    assert res.unreachable == [] and res.skipped_by_policy == []
    return res.postings


def test_jobgether_reads_public_api(policy: Policy) -> None:
    url = BOARD_ADAPTERS["jobgether"].url_for(BoardSearch(board="jobgether", term="ai engineer"))
    assert url.startswith("https://jobgether.com/astroapi/ai/jobs.json?keyword=ai+engineer&")
    first, _ = run("jobgether", "full stack developer", "jobgether.json", policy)
    assert (first.company, first.source) == ("Aimprosoft", "jobgether")
    assert first.location == "Anywhere (Full Remote)"
    assert first.experience_text == "Senior (5-10 years)"
    assert first.posted_date == date(2026, 10, 12)


def test_hirist_reads_search_json(policy: Policy) -> None:
    first, second = run("hirist", "full stack developer", "hirist.json", policy)
    assert (first.title, first.company) == (
        ".Net Full Stack Developer",
        "Capgemini Technology Services",
    )
    assert first.location == "Pune, Bangalore, Hyderabad"
    assert first.experience_text == "6-9 yrs"
    assert first.url == "https://www.hirist.tech/j/capgemini-net-full-stack-developer-1677117"
    assert second.posted_date is not None and second.stack_words


def test_cutshort_reads_next_data_and_strips_hidden_text(policy: Policy) -> None:
    url = BOARD_ADAPTERS["cutshort"].url_for(
        BoardSearch(board="cutshort", term="fullstack developer")
    )
    assert url == "https://cutshort.io/jobs/fullstack-developer-jobs"
    first, _ = run("cutshort", "fullstack developer", "cutshort.html", policy)
    assert first.title == "Full-Stack Engineer (AI-Native)"
    assert first.company == "appscrip"
    assert first.experience_text == "2 - 4 years"
    assert first.posted_date == date(2026, 10, 1)
    assert "Ignore previous" not in first.description


def test_bebee_reads_role_page_cards(policy: Policy) -> None:
    url = BOARD_ADAPTERS["bebee"].url_for(BoardSearch(board="bebee", term="AI Engineer"))
    assert url == "https://bebee.com/in/jobs/role/ai-engineer"
    first, second = run("bebee", "full stack developer", "bebee.html", policy)
    assert (first.title, first.company, first.location) == (
        "Junior Full Stack Developer", "INFINITE TECHNOLOGIES", "Bhuj, Gujarat",
    )  # fmt: skip
    assert first.url.startswith("https://bebee.com/in/jobs/junior-full-stack-developer-")
    assert second.location == "Chennai, Kolkata, Pune"


def test_switchly_reads_role_page_and_skips_signup_link(policy: Policy) -> None:
    postings = run("switchly", "ai engineer", "switchly.html", policy)
    assert [(p.company, p.location) for p in postings] == [
        ("Nagarro", "Remote · Remote"), ("Meesho", "Bangalore"),
    ]  # fmt: skip
    assert postings[0].title.startswith("Staff Engineer, ServiceNow")
    assert postings[0].url == "https://www.switchly.in/j/076b6b7a-6ecd-4cfe-8269-8bc25a7e258e"


@pytest.mark.parametrize("term", ["a/../x", "x?page=2", "", "ai&engineer"])
def test_unsafe_search_terms_are_refused(term: str) -> None:
    with pytest.raises(ValueError):
        BoardSearch(board="hirist", term=term)


def test_board_off_is_not_contacted(policy: Policy) -> None:
    policy.portals["hirist"].discover = False
    f = FakeFetcher({})
    res = discover_all([], f, policy, [BoardSearch(board="hirist", term="ai engineer")])
    assert f.calls == [] and len(res.skipped_by_policy) == 1


def test_searches_file_loads_and_every_url_is_on_an_allowed_host() -> None:
    searches = load_searches(REPO / "searches.yaml")
    assert searches
    for s in searches:
        host = BOARD_ADAPTERS[s.board].url_for(s).split("/")[2]
        assert host in KNOWN_BOARD_HOSTS


def test_jobleads_unpacks_nuxt_state(policy: Policy) -> None:
    first, second = run("jobleads", "full stack developer", "jobleads.html", policy)
    assert (first.title, first.company) == ("Full stack developer", "Wait4Ads")
    assert first.location == "Chennai District, Tamil Nadu"
    assert first.url.startswith("https://www.jobleads.com/in/job/full-stack-developer--")
    assert first.posted_date == date(2026, 6, 8)
    assert second.title == "Full Stack Developer (AWS)"


def test_wellfound_reads_apollo_state_and_maps_company(policy: Policy) -> None:
    first, second = run("wellfound", "software engineer", "wellfound.html", policy)
    assert first.url == "https://wellfound.com/jobs/4824775-software-engineer"
    assert (first.title, first.company) == ("Software Engineer", "Insight Consultants")
    assert first.location == "Bangalore Urban, Remote"
    assert first.experience_text == "1+ years"
    assert first.posted_date == date(2026, 10, 12)
    assert second.description == "Build APIs & agents."


def test_yc_reads_public_job_board_page(policy: Policy) -> None:
    url = BOARD_ADAPTERS["yc"].url_for(BoardSearch(board="yc", term="india"))
    assert url == "https://www.ycombinator.com/jobs/role/software-engineer/india"
    first, second = run("yc", "india", "yc.html", policy)
    assert (first.title, first.company, first.location) == (
        "QA / Hardware Test Engineer", "Human Archive", "IN",
    )  # fmt: skip
    assert first.url.startswith("https://www.ycombinator.com/companies/human-archive/jobs/")
    assert second.experience_text == "3+ years"


def test_boards_read_past_the_first_page_until_one_adds_nothing(policy: Policy) -> None:
    search = BoardSearch(board="jobgether", term="full stack developer")
    adapter = BOARD_ADAPTERS["jobgether"]
    page = (FIX / "boards" / "jobgether.json").read_text()
    empty = '{"jobs": []}'
    urls = adapter.page_urls(search)
    f = FakeFetcher(
        {urls[0]: FetchResult(200, page, urls[0]), urls[1]: FetchResult(200, empty, urls[1])}
    )
    res = discover_all([], f, policy, [search])
    assert f.calls == urls[:2] and len(res.postings) == 2 and res.unreachable == []


def test_cutshort_city_pages_are_all_read_even_when_one_adds_nothing(policy: Policy) -> None:
    search = BoardSearch(board="cutshort", term="fullstack developer")
    urls = BOARD_ADAPTERS["cutshort"].page_urls(search)
    page = (FIX / "boards" / "cutshort.html").read_text()
    f = FakeFetcher({url: FetchResult(200, page, url) for url in urls})  # all pages repeat
    discover_all([], f, policy, [search])
    assert f.calls == urls


def test_missing_later_pages_are_skipped_not_reported(policy: Policy) -> None:
    search = BoardSearch(board="cutshort", term="fullstack developer")
    urls = BOARD_ADAPTERS["cutshort"].page_urls(search)
    page = (FIX / "boards" / "cutshort.html").read_text()
    f = FakeFetcher({urls[0]: FetchResult(200, page, urls[0])})  # every city page 404s
    res = discover_all([], f, policy, [search])
    assert f.calls == urls and len(res.postings) == 2 and res.unreachable == []


def test_captcha_word_in_page_css_is_not_taken_for_a_bot_wall(policy: Policy) -> None:
    page = (FIX / "boards" / "cutshort.html").read_text()
    page = "<style>.grecaptcha-badge{display:none;}</style>" + page
    search = BoardSearch(board="cutshort", term="fullstack developer")
    url = BOARD_ADAPTERS["cutshort"].url_for(search)
    res = discover_all([], FakeFetcher({url: FetchResult(200, page, url)}), policy, [search])
    assert len(res.postings) == 2 and res.unreachable == []


def test_a_real_wall_page_is_still_reported(policy: Policy) -> None:
    search = BoardSearch(board="cutshort", term="fullstack developer")
    url = BOARD_ADAPTERS["cutshort"].url_for(search)
    wall = FetchResult(200, "<html><title>Just a moment...</title></html>", url)
    res = discover_all([], FakeFetcher({url: wall}), policy, [search])
    assert [u.reason for u in res.unreachable] == ["bot wall or captcha page"]
