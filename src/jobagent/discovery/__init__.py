"""Discovery adapters. Every request goes through the policy table first."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from jobagent.discovery.ashby import AshbyAdapter
from jobagent.discovery.base import (
    Adapter,
    BoardSearch,
    Company,
    DiscoveryResult,
    Fetcher,
)
from jobagent.discovery.bebee import BebeeAdapter
from jobagent.discovery.cutshort import CutshortAdapter
from jobagent.discovery.greenhouse import GreenhouseAdapter
from jobagent.discovery.hirist import HiristAdapter
from jobagent.discovery.jobgether import JobgetherAdapter
from jobagent.discovery.jobleads import JobleadsAdapter
from jobagent.discovery.lever import LeverAdapter
from jobagent.discovery.switchly import SwitchlyAdapter
from jobagent.discovery.wellfound import WellfoundAdapter
from jobagent.discovery.workable import WorkableAdapter
from jobagent.discovery.yc import YcAdapter
from jobagent.policy import Policy

ADAPTERS: dict[str, Adapter[Company]] = {
    a.portal: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), WorkableAdapter())
}
BOARD_ADAPTERS: dict[str, Adapter[BoardSearch]] = {
    a.portal: a
    for a in (
        JobgetherAdapter(), HiristAdapter(), CutshortAdapter(), BebeeAdapter(),
        SwitchlyAdapter(), JobleadsAdapter(), WellfoundAdapter(), YcAdapter(),
    )
}  # fmt: skip


def discover_all(
    companies: list[Company],
    fetcher: Fetcher,
    policy: Policy,
    searches: list[BoardSearch] | None = None,
) -> DiscoveryResult:
    """Each portal runs in its own thread, so one site's pauses never hold up another's."""
    jobs: dict[str, list[Callable[[], DiscoveryResult]]] = {}
    for company in companies:
        adapter = ADAPTERS[company.ats]
        jobs.setdefault(adapter.portal, []).append(
            partial(adapter.discover, company, fetcher, policy)
        )
    for search in searches or []:
        board = BOARD_ADAPTERS[search.board]
        jobs.setdefault(board.portal, []).append(partial(board.discover, search, fetcher, policy))

    def run_portal(tasks: list[Callable[[], DiscoveryResult]]) -> DiscoveryResult:
        out = DiscoveryResult()
        for task in tasks:
            out.extend(task())
        return out

    result = DiscoveryResult()
    if not jobs:
        return result
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for part in pool.map(run_portal, jobs.values()):
            result.extend(part)
    return result
