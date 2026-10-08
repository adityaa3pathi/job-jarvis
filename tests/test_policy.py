from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobagent.config import ConfigError, Settings, ensure_data_dir, load_secrets
from jobagent.lock import LockHeldError, RunLock
from jobagent.policy import Policy, PolicyDenied, engage_kill_switch
from tests.conftest import FakeClock


def test_unknown_portal_is_off(policy: Policy) -> None:
    for action in ("discover", "apply", "submit"):
        assert not policy.authorize("some-new-portal", action).allowed  # type: ignore[arg-type]


def test_defaults_match_research_table(policy: Policy) -> None:
    assert policy.rule("linkedin").mode == "off"
    assert policy.rule("indeed_india").mode == "off"
    for name in ("naukri", "foundit", "wellfound", "hirist", "cutshort"):
        assert policy.rule(name).mode == "discover_only"
    assert policy.rule("lever").mode == "discover_only"
    for name in ("greenhouse", "ashby", "workable"):
        assert policy.rule(name).mode == "apply_automatically"
        assert not policy.rule(name).live
    assert policy.dry_run


def test_discover_only_refuses_apply(policy: Policy) -> None:
    assert not policy.authorize("naukri", "apply").allowed
    assert not policy.authorize("naukri", "discover").allowed  # reads are off until enabled
    policy.portals["naukri"].discover = True
    assert policy.authorize("naukri", "discover").allowed


def test_dry_run_refuses_live_submit(policy: Policy) -> None:
    assert policy.authorize("greenhouse", "apply").allowed
    assert not policy.authorize("greenhouse", "submit", live=True).allowed
    policy.dry_run = False
    assert not policy.authorize("greenhouse", "submit", live=True).allowed  # portal not live
    policy.portals["greenhouse"].live = True
    assert not policy.authorize("greenhouse", "submit", live=False).allowed
    assert policy.authorize("greenhouse", "submit", live=True).allowed


def test_stop_file_halts_every_action(policy: Policy) -> None:
    assert policy.begin("greenhouse", "apply").allowed
    engage_kill_switch(policy.stop_file)
    for action in ("discover", "apply", "submit"):
        assert not policy.begin("greenhouse", action, live=True).allowed  # type: ignore[arg-type]


def test_stop_created_just_before_submit_blocks(policy: Policy) -> None:
    policy.dry_run = False
    policy.portals["greenhouse"].live = True
    assert policy.begin("greenhouse", "apply").allowed
    assert policy.authorize("greenhouse", "submit", live=True).allowed
    engage_kill_switch(policy.stop_file)  # appears after the last check
    with pytest.raises(PolicyDenied):
        policy.require("greenhouse", "submit", live=True)


def test_stop_found_from_another_directory(
    policy: Policy, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engage_kill_switch(settings.stop_file)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert policy.stopped()
    assert policy.stop_file.is_absolute()


def test_per_run_limit(policy: Policy) -> None:
    limit = policy.rule("greenhouse").per_run_limit
    for _ in range(limit):
        assert policy.begin("greenhouse", "apply").allowed
    assert not policy.begin("greenhouse", "apply").allowed


def test_min_pause_and_discover_rate(policy: Policy, clock: FakeClock) -> None:
    policy.begin("greenhouse", "apply")
    policy.begin("greenhouse", "apply")
    assert clock.slept == [20.0]
    for _ in range(50):
        assert policy.begin("lever", "discover").allowed
    assert sum(clock.slept) >= 20.0 + 49 * 1.0 - 1e-6


def test_stop_during_pause_blocks(policy: Policy, clock: FakeClock) -> None:
    policy.begin("greenhouse", "apply")

    def sleep_then_stop(seconds: float) -> None:
        clock.sleep(seconds)
        engage_kill_switch(policy.stop_file)

    policy.sleep = sleep_then_stop
    assert not policy.begin("greenhouse", "apply").allowed


def test_secrets_file_must_be_0600(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        load_secrets(settings, export=False)  # missing
    settings.config_dir.mkdir(parents=True)
    settings.secrets_file.write_text("GEMINI_API_KEY=abc123abc123\n")
    settings.secrets_file.chmod(0o644)
    with pytest.raises(ConfigError):
        load_secrets(settings, export=False)
    settings.secrets_file.chmod(0o600)
    assert load_secrets(settings, export=False) == {"GEMINI_API_KEY": "abc123abc123"}


def test_world_readable_data_dir_refused(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True)
    settings.data_dir.chmod(0o755)
    with pytest.raises(ConfigError):
        ensure_data_dir(settings)
    settings.data_dir.chmod(0o700)
    assert ensure_data_dir(settings) == settings.data_dir


def test_cloud_synced_data_dir_refused(settings: Settings, tmp_path: Path) -> None:
    settings.data_dir = tmp_path / "Dropbox" / "data"
    with pytest.raises(ConfigError):
        ensure_data_dir(settings)


def test_lock_refuses_second_run_and_clears_stale(settings: Settings) -> None:
    lock = RunLock(settings.lock_file)
    lock.acquire()
    with pytest.raises(LockHeldError):
        RunLock(settings.lock_file, pid=os.getpid()).acquire()
    lock.release()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    settings.lock_file.write_text(str(proc.pid))  # dead pid: stale
    RunLock(settings.lock_file).acquire()
