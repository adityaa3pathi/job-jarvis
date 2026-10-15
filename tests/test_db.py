"""Batch saves: few remote calls per posting, one transaction per portal (R2, R3)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from jobagent import cli
from jobagent.config import Settings, ensure_data_dir
from jobagent.db import Database
from jobagent.models import JobFilters, Posting


class CountingConn:
    """Wraps a sqlite3 connection and counts calls that would each be a Turso round trip."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.calls: Counter[str] = Counter()

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.calls["execute"] += 1
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> Any:
        self.calls["execute"] += 1
        return self._conn.executemany(sql, rows)

    def executescript(self, script: str) -> None:
        self.calls["executescript"] += 1
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CountingConn:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.calls["commit"] += 1
        self._conn.__exit__(*exc)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> list[CountingConn]:
    made: list[CountingConn] = []

    def fake_connect(path: Path | str) -> Any:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        made.append(CountingConn(conn))
        return made[-1]

    monkeypatch.setattr("jobagent.db.connect", fake_connect)
    return made


def posting(i: int = 1, **kw: Any) -> Posting:
    base: dict[str, Any] = {
        "source": "hirist", "url": f"https://www.hirist.tech/j/job-{i}", "company": f"Co{i}",
        "title": "AI Engineer", "location": "Bengaluru", "experience_text": "2-3 yrs",
    }  # fmt: skip
    base.update(kw)
    return Posting.model_validate(base)


def calls(db: Database) -> int:
    conn: CountingConn = db.conn  # type: ignore[assignment]
    return conn.calls["execute"]


def test_existing_posting_costs_at_most_three_calls(counting: list[CountingConn]) -> None:
    db = Database(":memory:")
    db.upsert_posting(posting())
    for again in (posting(), posting(posted_date=date(2026, 10, 1))):
        before = calls(db)
        db.upsert_posting(again)
        assert calls(db) - before <= 3, again  # read, write, one data-version bump


def test_closing_known_jobs_costs_two_calls_each_plus_the_shared_read(
    counting: list[CountingConn],
) -> None:
    db = Database(":memory:")
    db.upsert_many([posting(i) for i in range(600)])
    before = calls(db)
    results = db.upsert_many([posting(i, closed=True) for i in range(600)])
    assert {r.status for r in results} == {"closed"}
    # Update + status event each; two chunked reads; one data-version bump.
    assert calls(db) - before <= 2 * 600 + 3


def test_batch_reads_known_rows_once_and_commits_once(counting: list[CountingConn]) -> None:
    db = Database(":memory:")
    db.upsert_many([posting(i) for i in range(1000)])
    before, commits = calls(db), counting[0].calls["commit"]
    results = db.upsert_many([posting(i) for i in range(1000)])
    assert {r.outcome for r in results} == {"duplicate"}
    assert calls(db) - before <= 4  # two chunked reads, seen-date update, version bump
    assert counting[0].calls["commit"] - commits == 1


def test_batch_matches_one_by_one_outcomes() -> None:
    db = Database(":memory:")
    first = db.upsert_posting(posting(1))
    other_board = posting(1, source="cutshort", url="https://cutshort.io/job/x-1")
    results = db.upsert_many(
        [posting(1), other_board, posting(2), posting(2), posting(3, experience_text="10-12 yrs"),
         posting(1, posted_date=date(2026, 10, 1)), posting(1, closed=True)]
    )  # fmt: skip
    assert [r.outcome for r in results] == [
        "duplicate", "merged", "new", "duplicate", "new", "updated", "duplicate",
    ]  # fmt: skip
    assert results[1].job_id == first.job_id and results[3].job_id == results[2].job_id
    assert results[4].status == "dropped" and results[6].status == "closed"
    assert [r["new_status"] for r in db.events(first.job_id)] == ["found", "closed"]
    assert len(db.links(first.job_id)) == 2


def test_failed_batch_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(":memory:")

    def rules(p: Posting, *_: Any) -> None:
        if p.company == "Co2":
            raise ValueError("boom")

    monkeypatch.setattr("jobagent.db.check_hard_rules", rules)
    with pytest.raises(ValueError):
        db.upsert_many([posting(1), posting(2)])
    assert db.list_jobs() == []


def test_two_handles_saving_one_url_leave_one_job_and_one_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Database(tmp_path / "t.db"), Database(tmp_path / "t.db")
    # b read the table before a committed, so it believes the URL is new.
    monkeypatch.setattr(b, "_known", lambda keyed: ({}, {}))
    saved = a.upsert_posting(posting())
    again = b.upsert_posting(posting())
    assert (again.job_id, again.outcome) == (saved.job_id, "duplicate")
    count = lambda t: a.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert (count("jobs"), count("job_links"), count("events")) == (1, 1, 1)
    a.close()
    b.close()


def run(db: Database, seen: list[Posting], *, clean: bool = True) -> int:
    """One portal finishing: save what it read, then sweep what it did not."""
    db.upsert_many(seen)
    return db.close_vanished(seen, clean=clean, after_runs=3)


def ats(i: int, company: str = "Acme") -> Posting:
    token = company.lower()
    return posting(i, source="greenhouse", company=company, title=f"AI Engineer {i}",
                   url=f"https://job-boards.greenhouse.io/{token}/jobs/{i}")  # fmt: skip


def test_job_missing_from_three_clean_runs_is_closed() -> None:
    db = Database(":memory:")
    stays, goes = db.upsert_many([ats(1), ats(2)])
    for _ in range(2):
        assert run(db, [ats(1)]) == 0
    assert db.get_job(goes.job_id)["status"] == "found"
    assert run(db, [ats(1)]) == 1
    assert db.get_job(goes.job_id)["status"] == "closed"
    assert db.get_job(stays.job_id)["status"] == "found"
    assert db.events(goes.job_id)[-1]["new_status"] == "closed"


def test_errored_runs_and_reappearing_jobs_do_not_close() -> None:
    db = Database(":memory:")
    _, goes = db.upsert_many([ats(1), ats(2)])
    for _ in range(5):
        run(db, [ats(1)], clean=False)
    run(db, [ats(1)])
    run(db, [ats(1)])
    run(db, [ats(1), ats(2)])  # back again: the count starts over
    run(db, [ats(1)])
    run(db, [ats(1)])
    assert db.get_job(goes.job_id)["status"] == "found"


def test_other_portals_jobs_are_left_alone() -> None:
    db = Database(":memory:")
    other = db.upsert_posting(posting(9, source="cutshort", url="https://cutshort.io/job/9"))
    for _ in range(4):
        run(db, [ats(1)])
    assert db.get_job(other.job_id)["status"] == "found"


def test_old_database_gains_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, key TEXT NOT NULL, company TEXT NOT NULL,"
        " title TEXT NOT NULL, location TEXT NOT NULL, experience_text TEXT NOT NULL,"
        " stack_words TEXT NOT NULL, description TEXT NOT NULL, posted_date TEXT,"
        " seen_date TEXT NOT NULL, source TEXT NOT NULL, track TEXT, status TEXT NOT NULL,"
        " drop_reason TEXT, score INTEGER, rank_reason TEXT);"
        "INSERT INTO jobs VALUES (1,'k','Co','T','L','','[]','','',''"
        ",'hirist',NULL,'dropped','unpaid',NULL,NULL);"
    )
    old.commit()
    old.close()
    Database(path).close()
    db = Database(path)  # a second open must tolerate the columns already being there
    job = db.get_job(1)
    assert (job["rules_version"], job["missed_runs"]) == (1, 0)
    db.close()


def test_cli_recheck_prints_a_summary(settings: Settings, capsys: Any) -> None:
    ensure_data_dir(settings)
    db = Database(settings.db_path)
    saved = db.upsert_posting(posting(description="Unpaid leave on request."))
    with db.conn:
        db.conn.execute("UPDATE jobs SET status='dropped', drop_reason='unpaid', rules_version=1")
    db.close()
    assert cli.recheck(settings) == 0
    assert capsys.readouterr().out == "rechecked 1, undropped 1, dropped 0, merged 0\n"
    db = Database(settings.db_path)
    assert db.get_job(saved.job_id)["status"] == "found"
    db.close()


def test_search_board_jobs_are_never_auto_closed() -> None:
    # A search shows only a window of results; missing from it does not mean closed.
    db = Database(":memory:")
    board = [posting(i, source="hirist", url=f"https://www.hirist.tech/j/{i}") for i in (1, 2)]
    _, goes = db.upsert_many(board)
    for _ in range(4):
        assert run(db, board[:1]) == 0
    assert db.get_job(goes.job_id)["status"] == "found"


def test_a_company_missing_from_the_part_is_never_swept() -> None:
    # Bolt's board came back empty (or Bolt left companies.yaml): that proves nothing.
    db = Database(":memory:")
    _, bolt = db.upsert_many([ats(1), ats(2, "Bolt")])
    for _ in range(4):
        assert run(db, [ats(1)]) == 0
    job = db.get_job(bolt.job_id)
    assert (job["status"], job["missed_runs"]) == ("found", 0)


def test_a_job_closed_by_the_sweep_reopens_when_seen_again() -> None:
    db = Database(":memory:")
    _, goes = db.upsert_many([ats(1), ats(2)])
    for _ in range(3):
        run(db, [ats(1)])
    assert db.get_job(goes.job_id)["status"] == "closed"
    assert db.upsert_posting(ats(2)).status == "found"
    assert db.get_job(goes.job_id)["status"] == "found"
    last = db.events(goes.job_id)[-1]
    assert (last["old_status"], last["new_status"]) == ("closed", "found")


def test_a_job_the_source_closed_is_not_reopened() -> None:
    db = Database(":memory:")
    saved = db.upsert_posting(posting())
    assert db.upsert_posting(posting(closed=True)).status == "closed"
    assert db.upsert_posting(posting()).status == "closed"
    assert db.get_job(saved.job_id)["status"] == "closed"


def test_owner_back_to_list_survives_recheck_and_richer_text() -> None:
    db = Database(":memory:")
    senior = posting(experience_text="10-12 yrs")
    saved = db.upsert_posting(senior)
    assert saved.status == "dropped"
    db.set_status(saved.job_id, "found", "owner")  # "Back to list"; drop_reason stays
    db.recheck()
    assert db.get_job(saved.job_id)["status"] == "found"
    db.upsert_posting(posting(experience_text="10-12 yrs", description="Lead a big team."))
    assert db.get_job(saved.job_id)["status"] == "found"


def test_owner_drop_is_not_undone_by_recheck() -> None:
    db = Database(":memory:")
    saved = db.upsert_posting(posting(description="Unpaid leave on request."))
    with db.conn:
        db.conn.execute("UPDATE jobs SET status='dropped', drop_reason='unpaid', rules_version=1")
    db.set_status(saved.job_id, "dropped", "owner")
    db.recheck()
    assert db.get_job(saved.job_id)["status"] == "dropped"


DERIVED = ("min_years", "max_years", "role", "work_mode", "city")


def derived(db: Database, job_id: int) -> tuple[Any, ...]:
    job = db.get_job(job_id)
    return tuple(job[c] for c in DERIVED)


def test_a_saved_posting_gets_all_derived_fields() -> None:
    db = Database(":memory:")
    saved = db.upsert_posting(posting(title="Full Stack Developer", location="Bengaluru / Remote"))
    assert derived(db, saved.job_id) == (2, 3, "full_stack", "remote", "bangalore")
    plain = db.upsert_posting(posting(2, experience_text="", location="Pune"))
    assert derived(db, plain.job_id) == (None, None, "ai_ml", "onsite", "pune")


def test_recheck_fills_derived_fields_on_every_status() -> None:
    db = Database(":memory:")
    ids = [db.upsert_posting(posting(i)).job_id for i in range(1, 5)]
    db.set_status(ids[1], "applied", "owner")
    db.set_status(ids[2], "dropped", "owner")
    db.set_status(ids[3], "rejected")
    with db.conn:
        db.conn.execute(
            "UPDATE jobs SET min_years=NULL, max_years=NULL, role='', work_mode='', city=''"
        )
    db.recheck()
    for job_id in ids:
        assert derived(db, job_id) == (2, 3, "ai_ml", "onsite", "bangalore")
    assert [db.get_job(i)["status"] for i in ids] == ["found", "applied", "dropped", "rejected"]


def test_recheck_writes_derived_fields_in_one_call(counting: list[CountingConn]) -> None:
    db = Database(":memory:")
    db.upsert_many([posting(i) for i in range(50)])
    with db.conn:
        db.conn.execute("UPDATE jobs SET role=''")
    before = calls(db)
    db.recheck()
    first = calls(db) - before
    before = calls(db)
    db.recheck()
    assert first - (calls(db) - before) == 1  # the one backfill write; none when current


def test_re_seen_rows_with_current_fields_keep_the_shared_update(
    counting: list[CountingConn],
) -> None:
    db = Database(":memory:")
    db.upsert_many([posting(i) for i in range(300)])
    db.recheck()
    before = calls(db)
    db.upsert_many([posting(i) for i in range(300)])
    assert calls(db) - before <= 4


def test_an_unchanged_re_seen_row_skips_the_classifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(":memory:")
    db.upsert_posting(posting())
    used: list[str] = []
    monkeypatch.setattr("jobagent.db.role_of", lambda title: used.append(title) or "other")
    monkeypatch.setattr("jobagent.db.years_asked", lambda p: used.append(p.title))
    db.upsert_posting(posting())
    assert used == []


def test_re_seen_row_gets_stale_derived_fields_fixed() -> None:
    db = Database(":memory:")
    saved = db.upsert_posting(posting())
    with db.conn:
        db.conn.execute("UPDATE jobs SET role='', city=''")
    db.upsert_posting(posting())
    assert derived(db, saved.job_id) == (2, 3, "ai_ml", "onsite", "bangalore")


def test_a_repost_with_a_new_title_updates_the_role() -> None:
    db = Database(":memory:")
    saved = db.upsert_posting(posting())
    db.upsert_posting(posting(title="Frontend Developer", posted_date=date(2026, 10, 1)))
    assert db.get_job(saved.job_id)["role"] == "frontend"


def test_new_and_old_databases_get_derived_columns_and_indexes(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    Database(path).close()
    conn = sqlite3.connect(path)
    names = {r[1] for r in conn.execute("PRAGMA index_list(jobs)").fetchall()}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    conn.close()
    assert {"jobs_status_source", "jobs_status_posted"} <= names
    assert set(DERIVED) <= cols
    conn = sqlite3.connect(path)
    links = {r[1] for r in conn.execute("PRAGMA index_list(job_links)").fetchall()}
    conn.close()
    assert "job_links_job" in links


TODAY = date(2026, 10, 15)


def seeded(monkeypatch: pytest.MonkeyPatch) -> tuple[Database, dict[str, int]]:
    """Six found jobs and one applied, with today fixed at 15-10-2026 IST."""
    monkeypatch.setattr("jobagent.db.today_ist", lambda: TODAY)
    db = Database(":memory:")
    specs = {
        "ai": posting(1, title="AI Engineer", location="Bengaluru", experience_text="1-2 yrs",
                      posted_date=date(2026, 10, 14)),
        "fs": posting(2, title="Full Stack Developer", location="Remote",
                      experience_text="3-5 yrs", source="cutshort",
                      url="https://cutshort.io/job/2", posted_date=date(2026, 10, 13)),
        "fe": posting(3, title="Frontend Developer", location="Pune", experience_text="",
                      company="Acme 100% Co", source="greenhouse",
                      url="https://boards.greenhouse.io/acme/jobs/3"),
        "be": posting(4, title="Backend Engineer", location="Hybrid - Pune",
                      experience_text="", posted_date=date(2026, 9, 1),
                      description="Go services. You own billing_v2."),
        "mob": posting(5, title="Android Developer", location="Pune", experience_text="2+ years",
                       posted_date=date(2026, 10, 10)),
        "ml": posting(6, title="ML Engineer", location="Mumbai", experience_text="0-1 yrs",
                      posted_date=date(2026, 10, 15)),
        "done": posting(7, title="AI Engineer", location="Bengaluru",
                        posted_date=date(2026, 10, 15)),
    }  # fmt: skip
    ids = {name: db.upsert_many([p], now="2026-10-14T20:00:00+00:00")[0].job_id
           for name, p in specs.items()}  # fmt: skip
    db.set_status(ids["done"], "applied", "owner")
    return db, ids


def found(db: Database, **kw: Any) -> set[int]:
    rows, total = db.query_jobs(JobFilters(**kw), limit=100, offset=0)
    assert total == len(rows)
    return {r["id"] for r in rows}


def test_each_filter_narrows_found_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = seeded(monkeypatch)
    want = {k: ids[k] for k in ids}
    assert found(db) == {want[k] for k in ("ai", "fs", "fe", "be", "mob", "ml")}
    assert found(db, status="applied") == {want["done"]}
    assert found(db, source="cutshort") == {want["fs"]}
    assert found(db, role="backend") == {want["be"]}
    assert found(db, work_mode="hybrid") == {want["be"]}
    assert found(db, work_mode="remote") == {want["fs"]}
    assert found(db, city="pune") == {want["fe"], want["be"], want["mob"]}
    assert found(db, company="Acme 100% Co") == {want["fe"]}
    assert found(db, company="acme 100% co") == {want["fe"]}
    assert found(db, city="Pune") == {want["fe"], want["be"], want["mob"]}
    assert found(db, city="Bengaluru") == {want["ai"]}
    assert found(db, q="billing") == {want["be"]}
    assert found(db, q="android") == {want["mob"]}


def test_years_filter_with_and_without_unstated(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = seeded(monkeypatch)
    stated = {ids["ai"], ids["mob"], ids["ml"]}
    assert found(db, max_years=2) == stated | {ids["fe"], ids["be"]}
    assert found(db, max_years=2, include_unstated=False) == stated
    assert found(db, max_years=0, include_unstated=False) == {ids["ml"]}


def test_date_window_counts_days_in_ist(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = seeded(monkeypatch)
    # "fe" has no posted date, so its seen date (14-10) counts.
    assert found(db, posted_within_days=1) == {ids["ai"], ids["ml"], ids["fe"]}
    assert found(db, posted_within_days=3) == {ids["ai"], ids["ml"], ids["fe"], ids["fs"]}
    assert ids["be"] not in found(db, posted_within_days=30)


def test_filters_combine(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = seeded(monkeypatch)
    assert found(db, city="pune", max_years=3, include_unstated=False) == {ids["mob"]}
    assert found(db, city="pune", posted_within_days=7, q="developer") == {ids["fe"], ids["mob"]}
    assert found(db, role="ai_ml", source="hirist", posted_within_days=1) == {ids["ai"], ids["ml"]}


def test_search_treats_wildcards_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = seeded(monkeypatch)
    assert found(db, q="100%") == {ids["fe"]}
    assert found(db, q="%") == {ids["fe"]}
    assert found(db, q="Co_") == set()
    assert found(db, q="billing_v2") == {ids["be"]}


def test_source_counts_ignore_the_source_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = seeded(monkeypatch)
    counts = db.source_counts(JobFilters(source="cutshort"))
    assert counts == {"hirist": 4, "cutshort": 1, "greenhouse": 1}
    assert db.source_counts(JobFilters(role="frontend")) == {"greenhouse": 1}


def test_paging_walks_every_job_once_in_each_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = seeded(monkeypatch)
    with db.conn:  # equal sort keys everywhere, so only the id tie-break orders them
        db.conn.execute("UPDATE jobs SET posted_date='2026-10-01', min_years=NULL WHERE id > 3")
    every = found(db)
    for sort in ("newest", "fewest_years"):
        walked: list[int] = []
        for offset in range(0, 8, 2):
            rows, total = db.query_jobs(JobFilters(sort=sort), limit=2, offset=offset)
            assert total == len(every)
            walked += [r["id"] for r in rows]
        assert sorted(walked) == sorted(every), sort
    rows, total = db.query_jobs(JobFilters(), limit=2, offset=50)
    assert (rows, total) == ([], len(every))


def test_sorts_order_newest_and_fewest_years(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = seeded(monkeypatch)
    rows, _ = db.query_jobs(JobFilters(), limit=3, offset=0)
    assert [r["id"] for r in rows] == [ids["ml"], ids["fe"], ids["ai"]]
    rows, _ = db.query_jobs(JobFilters(sort="fewest_years"), limit=10, offset=0)
    assert [r["min_years"] for r in rows] == [0, 1, 2, 3, None, None]
    assert [r["id"] for r in rows][-2:] == [ids["fe"], ids["be"]]  # newest first among NULLs


def test_facets_list_companies_and_cities_by_count(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = seeded(monkeypatch)
    got = db.facets("found")
    assert got["cities"][0] == {"name": "pune", "count": 3}
    assert {c["name"] for c in got["cities"]} == {"pune", "bangalore", "mumbai"}
    assert {"name": "Acme 100% Co", "count": 1} in got["companies"]
    assert len(got["companies"]) == 6
    assert db.facets("applied")["cities"] == [{"name": "bangalore", "count": 1}]
