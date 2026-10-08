from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.config import REPO_ROOT, Settings, load_settings
from jobagent.policy import Policy


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")


@pytest.fixture
def policy(settings: Settings, clock: FakeClock) -> Policy:
    return Policy.load(
        REPO_ROOT / "policy.yaml", settings.stop_file, clock=clock, sleep=clock.sleep
    )


@pytest.fixture(scope="session")
def facts_path() -> Path:
    return REPO_ROOT / "facts.example.yaml"
