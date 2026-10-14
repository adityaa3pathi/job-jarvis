"""The per-portal save step the CLI and the web app share."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jobagent import cli
from jobagent.config import Settings, ensure_data_dir
from jobagent.db import Database
from jobagent.discovery import ADAPTERS
from jobagent.discovery.base import Company, DiscoveryResult, FetchResult
from jobagent.models import Posting
from jobagent.policy import Policy
from jobagent.runs import RunTally, save_portal
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
    return {"id": i, "title": f"AI Engineer {i}", "location": {"name": "Bengaluru"}}


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
