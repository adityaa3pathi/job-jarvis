"""The per-portal save step the CLI and the web app share."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from jobagent import cli
from jobagent.config import Settings, ensure_data_dir
from jobagent.db import Database
from jobagent.discovery import ADAPTERS
from jobagent.discovery.base import Company, DiscoveryResult, FetchResult, SweepContext
from jobagent.models import Posting
from jobagent.policy import Policy
from jobagent.runs import RunTally, rules_swept, save_portal, sweep_context
from jobagent.web.store import Store
from tests.test_discovery import FakeFetcher, saved_sources

GH = Company(name="Acme", ats="greenhouse", token="acme")


def part(source: str, company: str, n: int) -> DiscoveryResult:
    return DiscoveryResult(portal=source, postings=[
        Posting(source=source, url=f"https://{source}.example/{company}/{i}", company=company,
                title=f"AI Engineer {i}", location="Bengaluru") for i in range(n)
    ])  # fmt: skip


@pytest.mark.parametrize("broken", ["update_run", "close_vanished"])
def test_cli_bookkeeping_failure_never_stops_later_portals(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any, broken: str
) -> None:
    owner = Store if broken == "update_run" else Database
    real, calls = getattr(owner, broken), []

    def flaky(self: Any, *a: Any, **k: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("turso hiccup")
        return real(self, *a, **k)

    def fake_discover_all(*_: Any, on_portal_done: Any, **__: Any) -> DiscoveryResult:
        on_portal_done(part("greenhouse", "Acme", 1))
        on_portal_done(part("hirist", "Zed", 1))
        return DiscoveryResult()

    monkeypatch.setattr(owner, broken, flaky)
    monkeypatch.setattr("jobagent.cli.discover_all", fake_discover_all)
    assert cli.discover(settings) == 0
    assert "NEW  hirist" in capsys.readouterr().out
    assert saved_sources(settings.db_path) == {"greenhouse", "hirist"}
    st = Store(settings.db_path)
    last = st.runs()["last"]
    st.close()
    assert last is not None and last["status"] == "done"
    assert any("OSError: turso hiccup" in n for n in last["notes"])


def board(*jobs: dict[str, Any]) -> FetchResult:
    return FetchResult(200, json.dumps({"jobs": list(jobs)}), "x")


def gh_job(i: int) -> dict[str, Any]:
    return {"id": i, "title": f"AI Engineer {i}", "location": {"name": "Bengaluru"}, "content": ""}


def test_partial_reads_are_flagged(policy: Policy) -> None:
    adapter = ADAPTERS["greenhouse"]
    bad = board(gh_job(1), {"id": 2, "location": {}})  # no title: the record is skipped
    assert adapter.discover(GH, FakeFetcher({adapter.url_for(GH): bad}), policy).partial
    assert adapter.discover(GH, FakeFetcher({adapter.url_for(GH): board()}), policy).partial
    fine = board(gh_job(1), gh_job(2))
    assert not adapter.discover(GH, FakeFetcher({adapter.url_for(GH): fine}), policy).partial


def test_a_part_with_skipped_records_counts_no_misses(settings: Settings, policy: Policy) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    adapter, tally, run_id = ADAPTERS["greenhouse"], RunTally(), st.start_run()

    def read(page: FetchResult) -> None:
        got = adapter.discover(GH, FakeFetcher({adapter.url_for(GH): page}), policy)
        save_portal(got, db=db, st=st, run_id=run_id, tally=tally, after_runs=3)

    read(board(gh_job(1), gh_job(2)))
    for _ in range(4):
        read(board(gh_job(1), {"id": 2, "location": {}}))
    rows = db.conn.execute("SELECT status, missed_runs FROM jobs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("found", 0), ("found", 0)]
    db.close()
    st.close()


# --- conditional fetches and Greenhouse list-then-detail (U3) ---

GH_LIST = ADAPTERS["greenhouse"].url_for(GH)


def listed(i: int) -> dict[str, Any]:
    """A Greenhouse list item read without `content=true`: no job text."""
    return {"id": i, "title": f"AI Engineer {i}", "location": {"name": "Bengaluru"},
            "first_published": "2026-10-01T10:00:00-04:00"}  # fmt: skip


def detail_url(i: int) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/acme/jobs/{i}"


def detail(i: int) -> str:
    return json.dumps(listed(i) | {"content": f"&lt;p&gt;Build agents {i}. 1-3 years.&lt;/p&gt;"})


class Server:
    """Serves fixed bodies; answers 304 when the sent ETag matches (if it sends one)."""

    def __init__(self, pages: dict[str, str], etag: str | None = 'W/"v1"') -> None:
        self.pages, self.etag = pages, etag
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> FetchResult:
        self.calls.append((url, dict(headers or {})))
        if url not in self.pages:
            return FetchResult(404, "", url)
        tags = {"etag": self.etag} if self.etag else {}
        if self.etag and (headers or {}).get("If-None-Match") == self.etag:
            return FetchResult(304, "", url, tags)
        return FetchResult(200, self.pages[url], url, tags)


def sweep(
    db: Database, st: Store, server: Server, policy: Policy, *, cap: int = 200
) -> DiscoveryResult:
    """One run of the Acme board, saved the way the CLI saves it."""
    got = ADAPTERS["greenhouse"].discover(GH, server, policy, sweep_context(db, cap))
    tally = RunTally()
    save_portal(got, db=db, st=st, run_id=st.start_run(), tally=tally, after_runs=3)
    rules_swept(db, tally)
    return got


@pytest.mark.parametrize("etag", ['W/"v1"', None], ids=["304", "same-body"])
def test_unchanged_board_rewrites_no_rows_and_counts_no_miss(
    settings: Settings, policy: Policy, etag: str | None
) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    url = "https://job-boards.greenhouse.io/acme/jobs/9"
    gone = db.upsert_posting(Posting(source="greenhouse", company="Acme", title="Gone", url=url))
    server = Server({GH_LIST: board(gh_job(1), gh_job(2)).text}, etag)
    assert sweep(db, st, server, policy).postings  # changed: saved, and job 9 missed once
    with db.conn:
        db.conn.execute("UPDATE jobs SET seen_date='old'")
    version = db.data_version()
    for _ in range(4):
        got = sweep(db, st, server, policy)
        assert (got.unchanged, got.postings, got.partial) == (["Acme"], [], False)
    rows = db.conn.execute("SELECT status, seen_date FROM jobs").fetchall()
    assert {(r["status"], r["seen_date"]) for r in rows} == {("found", "old")}
    assert db.get_job(gone.job_id)["missed_runs"] == 1
    assert db.data_version() == version
    if etag:
        assert server.calls[-1][1] == {"If-None-Match": etag}
    db.close()
    st.close()


def test_validators_are_kept_only_after_the_save_succeeds(
    settings: Settings, policy: Policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    server = Server({GH_LIST: board(gh_job(1)).text})

    def down(postings: Any) -> Any:
        raise OSError("down")

    with monkeypatch.context() as m:
        m.setattr(db, "upsert_many", down)
        sweep(db, st, server, policy)
    got = sweep(db, st, server, policy)  # nothing was kept, so it is read and saved now
    assert got.unchanged == [] and saved_sources(settings.db_path) == {"greenhouse"}
    assert sweep(db, st, server, policy).unchanged == ["Acme"]
    db.close()
    st.close()


def test_only_new_greenhouse_ids_get_detail_fetches(settings: Settings, policy: Policy) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    sweep(db, st, Server({GH_LIST: board(listed(1)).text, detail_url(1): detail(1)}), policy)
    server = Server({GH_LIST: board(listed(1), listed(2)).text, detail_url(2): detail(2)}, None)
    got = sweep(db, st, server, policy)
    assert [url for url, _ in server.calls] == [GH_LIST, detail_url(2)]
    assert [p.description for p in got.postings] == ["", "Build agents 2. 1-3 years."]
    texts = [r[0] for r in db.conn.execute("SELECT description FROM jobs ORDER BY id")]
    assert texts == ["Build agents 1. 1-3 years.", "Build agents 2. 1-3 years."]
    db.close()
    st.close()


def test_capped_greenhouse_ids_are_not_saved(settings: Settings, policy: Policy) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    pages = {GH_LIST: board(listed(1), listed(2), listed(3)).text}
    pages |= {detail_url(i): detail(i) for i in (1, 2, 3)}
    server = Server(pages)
    got = sweep(db, st, server, policy, cap=1)
    assert [url for url, _ in server.calls] == [GH_LIST, detail_url(1)]
    assert any("capped" in n for n in got.notes) and not got.partial
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    got = sweep(db, st, server, policy)  # the board's cache entry was left stale on purpose
    assert got.unchanged == [] and server.calls[-3] == (GH_LIST, {})
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    db.close()
    st.close()


def test_a_failed_detail_fetch_is_not_saved_and_is_read_next_run(
    settings: Settings, policy: Policy
) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    pages = {GH_LIST: board(listed(1), listed(2)).text, detail_url(1): detail(1)}
    server = Server(pages)  # job 2's page is missing: a 404
    got = sweep(db, st, server, policy)
    assert any("could not be read" in n for n in got.notes)
    assert [r[0] for r in db.conn.execute("SELECT title FROM jobs")] == ["AI Engineer 1"]
    assert db.fetch_validators() == {}
    server.pages[detail_url(2)] = detail(2)
    got = sweep(db, st, server, policy)
    assert got.unchanged == [] and server.calls[-2:] == [(GH_LIST, {}), (detail_url(2), {})]
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert GH_LIST in db.fetch_validators()
    db.close()
    st.close()


def test_a_rules_bump_rereads_unchanged_boards_once(
    settings: Settings, policy: Policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_data_dir(settings)
    db, st = Database(settings.db_path), Store(settings.db_path)
    server = Server({GH_LIST: board(gh_job(1), gh_job(2)).text})
    sweep(db, st, server, policy)
    assert sweep(db, st, server, policy).unchanged == ["Acme"]
    with db.conn:  # dropped by an old rule the new version no longer has
        db.conn.execute(
            "UPDATE jobs SET status='dropped', drop_reason='too_senior', rules_version=1 WHERE id=1"
        )
    monkeypatch.setattr("jobagent.db.RULES_VERSION", 99)
    got = sweep(db, st, server, policy)
    assert got.unchanged == [] and server.calls[-1] == (GH_LIST, {})
    assert db.get_job(1)["status"] == "found" and db.get_job(1)["rules_version"] == 99
    assert sweep(db, st, server, policy).unchanged == ["Acme"]
    db.close()
    st.close()


def test_detail_slots_are_never_over_spent_across_threads() -> None:
    import threading

    ctx = SweepContext(detail_cap=10)
    got: list[bool] = []
    lock = threading.Lock()

    def take() -> None:
        for _ in range(20):
            ok = ctx.take_detail()
            with lock:
                got.append(ok)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 10 and ctx.details_used == 10
