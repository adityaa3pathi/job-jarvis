"""Discovery adapters. Every request goes through the policy table first."""

from __future__ import annotations

from jobagent.discovery.ashby import AshbyAdapter
from jobagent.discovery.base import Adapter, Company, DiscoveryResult, Fetcher
from jobagent.discovery.greenhouse import GreenhouseAdapter
from jobagent.discovery.lever import LeverAdapter
from jobagent.discovery.workable import WorkableAdapter
from jobagent.policy import Policy

ADAPTERS: dict[str, Adapter] = {
    a.portal: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), WorkableAdapter())
}


def discover_all(companies: list[Company], fetcher: Fetcher, policy: Policy) -> DiscoveryResult:
    result = DiscoveryResult()
    for company in companies:
        result.extend(ADAPTERS[company.ats].discover(company, fetcher, policy))
    return result
