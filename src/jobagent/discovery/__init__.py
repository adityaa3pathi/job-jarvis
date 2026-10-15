"""Discovery adapters. Every request goes through the policy table first."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from functools import partial

from jobagent.discovery.ashby import AshbyAdapter
from jobagent.discovery.base import (
    Adapter,
    BoardSearch,
    Company,
    DiscoveryResult,
    Fetcher,
    SweepContext,
    Unreachable,
)
from jobagent.discovery.bebee import BebeeAdapter
from jobagent.discovery.cutshort import CutshortAdapter
from jobagent.discovery.greenhouse import GreenhouseAdapter
from jobagent.discovery.himalayas import HimalayasAdapter
from jobagent.discovery.hirist import HiristAdapter
from jobagent.discovery.hn import HnAdapter
from jobagent.discovery.jobgether import JobgetherAdapter
from jobagent.discovery.jobicy import JobicyAdapter
from jobagent.discovery.jobleads import JobleadsAdapter
from jobagent.discovery.lever import LeverAdapter
from jobagent.discovery.linkedin import LinkedinAdapter
from jobagent.discovery.remoteok import RemoteokAdapter
from jobagent.discovery.remotive import RemotiveAdapter
from jobagent.discovery.smartrecruiters import SmartrecruitersAdapter
from jobagent.discovery.switchly import SwitchlyAdapter
from jobagent.discovery.wellfound import WellfoundAdapter
from jobagent.discovery.weworkremotely import WeworkremotelyAdapter
from jobagent.discovery.workable import WorkableAdapter
from jobagent.discovery.workingnomads import WorkingnomadsAdapter
from jobagent.discovery.yc import YcAdapter
from jobagent.policy import Policy

ADAPTERS: dict[str, Adapter[Company]] = {
    a.portal: a
    for a in (
        GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), WorkableAdapter(),
        SmartrecruitersAdapter(),
    )
}  # fmt: skip
BOARD_ADAPTERS: dict[str, Adapter[BoardSearch]] = {
    a.portal: a
    for a in (
        JobgetherAdapter(), HiristAdapter(), CutshortAdapter(), BebeeAdapter(),
        SwitchlyAdapter(), JobleadsAdapter(), WellfoundAdapter(), YcAdapter(), LinkedinAdapter(),
        HimalayasAdapter(), RemoteokAdapter(), JobicyAdapter(), RemotiveAdapter(),
        WeworkremotelyAdapter(), WorkingnomadsAdapter(), HnAdapter(),
    )
}  # fmt: skip


def discover_all(
    companies: list[Company],
    fetcher: Fetcher,
    policy: Policy,
    searches: list[BoardSearch] | None = None,
    on_portal_done: Callable[[DiscoveryResult], None] | None = None,
    ctx: SweepContext | None = None,
) -> DiscoveryResult:
    """Each portal runs in its own thread, so one site's pauses never hold up another's.
    `on_portal_done` gets each portal's result as it finishes, always on the calling thread.
    `ctx` lets company boards skip unchanged lists and fetch text for new jobs only."""
    jobs: dict[str, list[tuple[str, Callable[[], DiscoveryResult]]]] = {}
    # India job boards first: they hold most of the useful jobs.
    for search in searches or []:
        board = BOARD_ADAPTERS[search.board]
        jobs.setdefault(board.portal, []).append(
            (search.name, partial(board.discover, search, fetcher, policy))
        )
    extra = {"ctx": ctx} if ctx else {}
    for company in companies:
        adapter = ADAPTERS[company.ats]
        jobs.setdefault(adapter.portal, []).append(
            (company.name, partial(adapter.discover, company, fetcher, policy, **extra))
        )

    def run_portal(
        portal: str, tasks: list[tuple[str, Callable[[], DiscoveryResult]]]
    ) -> DiscoveryResult:
        out = DiscoveryResult(portal=portal)
        for done, (name, task) in enumerate(tasks, 1):
            try:
                out.extend(task())
            except Exception as exc:  # one target's error must never lose the portal's other jobs
                out.unreachable.append(Unreachable(name, "", f"{type(exc).__name__}: {exc}"))
            stop = out.blocked or (out.rate_limited and portal in BOARD_ADAPTERS)
            if stop and (left := len(tasks) - done):
                # A refusal is sticky: asking again in the same run only invites a ban
                noun = "target" if left == 1 else "targets"
                out.notes.append(f"{portal}: blocked, {left} remaining {noun} skipped")
                out.partial = True
                break
        return out

    result = DiscoveryResult()
    if not jobs:
        return result
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run_portal, name, tasks) for name, tasks in jobs.items()]
        parts: dict[Future[DiscoveryResult], DiscoveryResult] = {}
        for future in as_completed(futures):
            parts[future] = future.result()
            if on_portal_done:
                on_portal_done(parts[future])
    for future in futures:  # merged in schedule order, so the result does not depend on timing
        result.extend(parts[future])
    return result
