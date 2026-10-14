"""Discovery adapters. Every request goes through the policy table first."""

from __future__ import annotations

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
    result = DiscoveryResult()
    for company in companies:
        result.extend(ADAPTERS[company.ats].discover(company, fetcher, policy))
    for search in searches or []:
        result.extend(BOARD_ADAPTERS[search.board].discover(search, fetcher, policy))
    return result
