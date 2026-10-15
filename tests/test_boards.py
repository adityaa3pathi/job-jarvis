from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from jobagent.discovery import ADAPTERS, BOARD_ADAPTERS, discover_all
from jobagent.discovery.base import BoardSearch, Company, FetchResult, load_searches
from jobagent.discovery.fetch import KNOWN_BOARD_HOSTS
from jobagent.policy import Policy, engage_kill_switch
from tests.test_discovery import FIX, GH, FakeFetcher, ok

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
    assert first.location == "Bangalore Urban / Remote"
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


def test_empty_cutshort_queries_do_not_lose_greenhouse_jobs(policy: Policy) -> None:
    search = BoardSearch(board="cutshort", term="fullstack developer")
    url = BOARD_ADAPTERS["cutshort"].url_for(search)
    data = {"props": {"pageProps": {"dehydratedState": {"queries": []}}}}
    page = f'<script id="__NEXT_DATA__">{json.dumps(data)}</script>'
    gh_url = ADAPTERS["greenhouse"].url_for(GH)
    f = FakeFetcher(
        {url: FetchResult(200, page, url), gh_url: ok("api/greenhouse_acme.json", gh_url)}
    )
    res = discover_all([GH], f, policy, [search])
    assert len(res.postings) == 2 and {p.source for p in res.postings} == {"greenhouse"}
    assert [u.company for u in res.unreachable] == ["cutshort: fullstack developer"]


@pytest.mark.parametrize("error", [IndexError("list index out of range"), RuntimeError("boom")])
def test_any_parse_error_is_unreachable_and_other_portals_still_return(
    policy: Policy, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(*_: object) -> list:  # type: ignore[type-arg]
        raise error

    monkeypatch.setattr(BOARD_ADAPTERS["hirist"], "parse", broken)
    hirist = BoardSearch(board="hirist", term="ai engineer")
    h_url = BOARD_ADAPTERS["hirist"].url_for(hirist)
    jg = BoardSearch(board="jobgether", term="full stack developer")
    jg_url = BOARD_ADAPTERS["jobgether"].url_for(jg)
    f = FakeFetcher(
        {
            h_url: FetchResult(200, '{"data": []}', h_url),
            jg_url: FetchResult(200, (FIX / "boards" / "jobgether.json").read_text(), jg_url),
        }
    )
    res = discover_all([], f, policy, [hirist, jg])
    assert len(res.postings) == 2
    (bad,) = res.unreachable
    assert bad.company == "hirist: ai engineer" and type(error).__name__ in bad.reason


def test_a_task_that_raises_is_unreachable_not_a_crash(
    policy: Policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(BOARD_ADAPTERS["hirist"], "discover", broken)
    hirist = BoardSearch(board="hirist", term="ai engineer")
    res = discover_all([], FakeFetcher({}), policy, [hirist])
    assert [(u.company, u.reason) for u in res.unreachable] == [
        ("hirist: ai engineer", "RuntimeError: boom")
    ]


def test_one_bad_record_is_skipped_with_a_note(policy: Policy) -> None:
    good = json.loads((FIX / "boards" / "jobgether.json").read_text())["jobs"]
    bad = {**good[0], "url": "https://jobgether.com/offer/x", "company": None}
    search = BoardSearch(board="jobgether", term="ai engineer")
    url = BOARD_ADAPTERS["jobgether"].url_for(search)
    page = FetchResult(200, json.dumps({"jobs": [good[0], bad, good[1]]}), url)
    res = discover_all([], FakeFetcher({url: page}), policy, [search])
    assert len(res.postings) == 2 and res.unreachable == []
    assert res.notes == ["jobgether: ai engineer: 1 record skipped (ValidationError)"]


# --- run report notes (R9) ---


def test_a_target_with_no_postings_on_page_one_gets_a_check_layout_note(policy: Policy) -> None:
    search = BoardSearch(board="bebee", term="python")
    url = BOARD_ADAPTERS["bebee"].url_for(search)
    page = FetchResult(200, "<html><body><main>No cards here</main></body></html>", url)
    res = discover_all([], FakeFetcher({url: page}), policy, [search])
    assert res.postings == [] and res.unreachable == []
    assert res.notes == ["bebee: python: 0 jobs (check layout)"]
    assert res.per_target == {"bebee: python": 0}


def test_per_target_counts_are_kept(policy: Policy) -> None:
    search = BoardSearch(board="jobgether", term="full stack developer")
    url = BOARD_ADAPTERS["jobgether"].url_for(search)
    page = FetchResult(200, (FIX / "boards" / "jobgether.json").read_text(), url)
    res = discover_all([], FakeFetcher({url: page}), policy, [search])
    assert res.per_target == {"jobgether: full stack developer": 2} and res.notes == []


def _jobgether_pages(more: bool) -> tuple[BoardSearch, dict[str, FetchResult]]:
    search = BoardSearch(board="jobgether", term="ai engineer")
    job = json.loads((FIX / "boards" / "jobgether.json").read_text())["jobs"][0]
    pages = {}
    for n, url in enumerate(BOARD_ADAPTERS["jobgether"].page_urls(search)):
        jobs = [{**job, "url": f"https://jobgether.com/offer/{n}-{i}"} for i in range(25)]
        body = {"jobs": jobs, "pagination": {"page": n + 1, "limit": 25, "hasMore": more}}
        pages[url] = FetchResult(200, json.dumps(body), url)
    return search, pages


@pytest.mark.parametrize("more", [True, False])
def test_a_full_last_page_with_more_results_is_noted_as_truncated(
    policy: Policy, more: bool
) -> None:
    search, pages = _jobgether_pages(more)
    res = discover_all([], FakeFetcher(pages), policy, [search])
    assert len(res.postings) == 100
    assert res.notes == (["jobgether: ai engineer: truncated at 4 pages"] if more else [])


def test_kill_switch_mid_run_is_noted(policy: Policy) -> None:
    search, pages = _jobgether_pages(True)

    class StopAfterFirst(FakeFetcher):
        def get(self, url: str) -> FetchResult:
            engage_kill_switch(policy.stop_file)
            return super().get(url)

    res = discover_all([], StopAfterFirst(pages), policy, [search])
    assert len(res.postings) == 25
    assert res.notes == ["jobgether: ai engineer: stopped by kill switch after page 1"]


# --- LinkedIn logged-out job list (plan U2) ---

LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def test_linkedin_reads_guest_cards_and_strips_tracking(policy: Policy) -> None:
    jobs = run("linkedin", "python developer", "linkedin.html", policy)
    assert [(p.title, p.company, p.location) for p in jobs] == [
        ("Python Developer", "Infosys", "Bengaluru East, Karnataka, India"),
        ("Python Developer | Django/Flask | 5-12 Years", "Tata Consultancy Services",
         "Bengaluru, Karnataka, India"),
        ("Python Developer", "Algorithus", "Greater Lucknow Area"),
    ]  # fmt: skip
    assert [p.posted_date for p in jobs] == [
        date(2026, 10, 10), date(2026, 10, 8), date(2026, 10, 9),
    ]  # fmt: skip
    assert jobs[0].url == "https://in.linkedin.com/jobs/view/python-developer-at-infosys-4468058393"
    assert jobs[2].url == "https://in.linkedin.com/jobs/view/python-developer-4476243793"
    assert {p.source for p in jobs} == {"linkedin"}


def test_linkedin_reads_three_pages_of_ten_at_most() -> None:
    adapter = BOARD_ADAPTERS["linkedin"]
    urls = adapter.page_urls(BoardSearch(board="linkedin", term="ai engineer"))
    assert urls == [
        f"{LI_SEARCH}?keywords=ai+engineer&location=India&start={n}" for n in (0, 10, 20)
    ]
    assert adapter.paged and adapter.page_size == 10 and not adapter.expect_json


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (999, ""),
        (429, ""),
        (200, '<html><head><title>Sign Up | LinkedIn</title></head><body class="authwall">'),
    ],
)
def test_a_blocked_linkedin_page_stops_the_board_for_the_run(
    policy: Policy, status: int, body: str
) -> None:
    first = BoardSearch(board="linkedin", term="python developer")
    rest = [BoardSearch(board="linkedin", term=t) for t in ("ai engineer", "react developer")]
    jg = BoardSearch(board="jobgether", term="full stack developer")
    jg_url = BOARD_ADAPTERS["jobgether"].url_for(jg)
    url = BOARD_ADAPTERS["linkedin"].url_for(first)
    f = FakeFetcher(
        {url: FetchResult(status, body, url), jg_url: ok("boards/jobgether.json", jg_url)}
    )
    res = discover_all([], f, policy, [first, *rest, jg])
    assert [c for c in f.calls if "linkedin" in c] == [url]
    assert [u.company for u in res.unreachable] == ["linkedin: python developer"]
    assert "linkedin: blocked, 2 remaining targets skipped" in res.notes
    assert len(res.postings) == 2  # other boards go on


def test_a_block_on_any_board_skips_its_remaining_targets(policy: Policy) -> None:
    a, b = (BoardSearch(board="hirist", term=t) for t in ("ai engineer", "python developer"))
    url = BOARD_ADAPTERS["hirist"].url_for(a)
    f = FakeFetcher({url: FetchResult(429, "", url)})
    res = discover_all([], f, policy, [a, b])
    assert f.calls == [url] and res.partial
    assert res.notes == ["hirist: blocked, 1 remaining target skipped"]


def test_a_429_on_one_company_board_never_skips_the_other_companies(policy: Policy) -> None:
    busy = Company(name="Busy", ats="greenhouse", token="busy")
    busy_url, later_url = (ADAPTERS["greenhouse"].url_for(c) for c in (busy, GH))
    f = FakeFetcher({
        busy_url: FetchResult(429, "", busy_url),
        later_url: ok("api/greenhouse_acme.json", later_url),
    })  # fmt: skip
    res = discover_all([busy, GH], f, policy)
    assert later_url in f.calls and res.postings
    assert [u.company for u in res.unreachable] == ["Busy"]
    assert not any("blocked" in n for n in res.notes)


def test_an_ordinary_error_does_not_stop_the_board(policy: Policy) -> None:
    a, b = (BoardSearch(board="hirist", term=t) for t in ("ai engineer", "python developer"))
    f = FakeFetcher({})  # 404 on both
    discover_all([], f, policy, [a, b])
    assert len(f.calls) == 2


def test_linkedin_searches_load_and_hosts_are_allowed() -> None:
    searches = load_searches(REPO / "searches.yaml")
    linkedin = [s.term for s in searches if s.board == "linkedin"]
    assert linkedin and linkedin == [s.term for s in searches if s.board == "jobgether"]
    assert {"www.linkedin.com", "in.linkedin.com"} <= KNOWN_BOARD_HOSTS


# --- remote boards and SmartRecruiters (remote and India boards plan) ---


def test_himalayas_reads_india_open_jobs(policy: Policy) -> None:
    url = BOARD_ADAPTERS["himalayas"].url_for(BoardSearch(board="himalayas", term="ai engineer"))
    assert url == "https://himalayas.app/jobs/api/search?q=ai+engineer&country=IN&page=1"
    india, anywhere = run("himalayas", "python developer", "himalayas.json", policy)
    assert (india.title, india.company, india.location) == (
        "Senior Python Developer", "ITHR Technologies Consulting LLC", "Remote (India)",
    )  # fmt: skip
    assert india.posted_date == date(2026, 10, 9) and india.experience_text == "Senior"
    assert anywhere.location == "Remote (Worldwide)" and anywhere.description


def test_remoteok_skips_the_legal_notice_and_keeps_its_own_link(policy: Policy) -> None:
    first, second = run("remoteok", "python", "remoteok.json", policy)
    assert (first.title, first.company, first.location) == (
        "Principal Software Engineer", "Vytalize Health", "Remote (Kansas)",
    )  # fmt: skip
    assert first.url.startswith("https://remoteok.com/remote-jobs/")
    assert second.location == "Remote"


def test_jobicy_reads_region_as_remote_location(policy: Policy) -> None:
    apac, canada = run("jobicy", "python", "jobicy.json", policy)
    assert (apac.company, apac.location, apac.experience_text) == (
        "Binance", "Remote (APAC)", "Entry-Level, Junior",
    )  # fmt: skip
    assert canada.location == "Remote (Canada)"
    assert apac.url.startswith("https://jobicy.com/jobs/")


def test_remotive_keeps_tech_categories_only(policy: Policy) -> None:
    (job,) = run("remotive", "software-dev", "remotive.json", policy)
    assert (job.title, job.company) == ("Senior React Full-stack Developer", "Lemon.io")
    assert job.location == "Remote (Europe, USA, Canada, APAC)"
    assert job.url.startswith("https://remotive.com/remote-jobs/")


def test_weworkremotely_splits_company_from_title_in_rss(policy: Policy) -> None:
    first, second = run(
        "weworkremotely", "remote-full-stack-programming-jobs", "weworkremotely.xml", policy
    )
    assert (first.title, first.company, first.location) == (
        "AI Experience Engineer (Conversational AI)", "Upgrade", "Remote (Anywhere in the World)",
    )  # fmt: skip
    assert first.posted_date == date(2026, 10, 15)
    assert (second.title, second.company) == ("Senior Java Developer", "Twikey")


def test_workingnomads_keeps_the_asked_category_only(policy: Policy) -> None:
    (job,) = run("workingnomads", "development", "workingnomads.json", policy)
    assert (job.title, job.company) == ("Senior AI Engineer / Architect", "Lemon.io")
    assert job.location == "Remote (Europe, LATAM, APAC, the U.S., Canada)"


def test_hn_finds_this_months_thread_and_keeps_remote_top_level_posts(policy: Policy) -> None:
    from jobagent.discovery.hn import THREADS

    search = BoardSearch(board="hn", term="remote")
    adapter = BOARD_ADAPTERS["hn"]
    adapter._story = "49922569"  # type: ignore[attr-defined]
    comments = adapter.url_for(search)
    adapter._story = ""  # type: ignore[attr-defined]
    f = FakeFetcher(
        {
            THREADS: ok("boards/hn_threads.json", THREADS),
            comments: ok("boards/hn_comments.json", comments),
        }
    )
    res = discover_all([], f, policy, [search])
    assert f.calls == [THREADS, comments] and res.unreachable == []
    (job,) = res.postings  # the onsite Helsinki post and the reply are left out
    assert (job.company, job.location) == ("Irash Tech", "REMOTE (Worldwide)")
    assert job.title == "Business Development Manager (Freelance, Commission-only)"
    assert job.url == "https://news.ycombinator.com/item?id=50068986"


def test_hn_without_a_hiring_thread_is_unreachable(policy: Policy) -> None:
    from jobagent.discovery.hn import THREADS

    f = FakeFetcher({THREADS: FetchResult(200, '{"hits": []}', THREADS)})
    res = discover_all([], f, policy, [BoardSearch(board="hn", term="remote")])
    assert f.calls == [THREADS] and res.postings == []
    assert [u.company for u in res.unreachable] == ["hn: remote"]


def test_smartrecruiters_reads_india_postings_and_fetches_job_text(policy: Policy) -> None:
    company = Company(name="Freshworks", ats="smartrecruiters", token="Freshworks")
    adapter = ADAPTERS["smartrecruiters"]
    url = adapter.url_for(company)
    assert url.endswith("/Freshworks/postings?country=in&limit=100&offset=0")
    api = "https://api.smartrecruiters.com/v1/companies/Freshworks/postings"
    f = FakeFetcher({
        url: ok("boards/smartrecruiters.json", url),
        f"{api}/744000155675855": ok("boards/smartrecruiters_detail.json", api),
    })  # fmt: skip
    res = discover_all([company], f, policy)
    (job,) = res.postings  # the second job's text could not be read, so it waits for next run
    assert (job.title, job.location) == (
        "Staff Engineer - Machine Learning",
        "Bengaluru, KA, India",
    )
    assert job.url == "https://jobs.smartrecruiters.com/Freshworks/744000155675855"
    assert job.description.startswith("The Impact You Will Create")


@pytest.mark.parametrize(
    ("location", "dropped"),
    [
        ("Remote (India)", False),
        ("Remote (Worldwide)", False),
        ("Remote (Europe, USA, Canada, APAC)", False),
        ("Remote (Anywhere in the World)", False),
        ("Remote", False),
        ("Remote (Canada)", True),
        ("Remote (USA)", True),
        ("REMOTE (US only, US time zones; no visa sponsorship)", True),
    ],
)
def test_remote_board_locations_meet_the_india_rule(location: str, dropped: bool) -> None:
    from jobagent.rules import outside_india

    assert outside_india(location) is dropped


def test_instahyre_reads_logged_out_search_data(policy: Policy) -> None:
    url = BOARD_ADAPTERS["instahyre"].url_for(BoardSearch(board="instahyre", term="full stack"))
    assert url == "https://www.instahyre.com/api/v1/job_search?skills=full+stack&limit=35&offset=0"
    first, second = run("instahyre", "python", "instahyre.json", policy)
    assert (first.title, first.company, first.location) == (
        "Python Engineer", "Nineleaps", "Bangalore",
    )  # fmt: skip
    assert first.url.startswith("https://www.instahyre.com/job-439850-")
    assert second.location == "Bangalore,Hyderabad" and "Python" in first.stack_words
