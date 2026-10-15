from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from jobagent import cli
from jobagent.config import REPO_ROOT, ConfigError, Settings, ensure_data_dir, load_secrets
from jobagent.lock import STALE_AFTER_S, LockHeldError, RunLock
from jobagent.policy import Policy, PolicyDenied, engage_kill_switch
from jobagent.runs import kill_switch_check
from tests.conftest import FakeClock


def test_unknown_portal_is_off(policy: Policy) -> None:
    for action in ("discover", "apply", "submit"):
        assert not policy.authorize("some-new-portal", action).allowed  # type: ignore[arg-type]


def test_defaults_match_research_table(policy: Policy) -> None:
    assert policy.rule("indeed_india").mode == "off"
    for name in ("linkedin", "naukri", "foundit", "wellfound", "hirist", "cutshort"):
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


def test_lock_held_too_long_is_stale_even_when_the_pid_is_alive(settings: Settings) -> None:
    # Containers reuse low pids, so a live pid alone does not prove the holder is a run.
    old = time.time() - STALE_AFTER_S - 60
    settings.lock_file.parent.mkdir(parents=True)
    settings.lock_file.write_text(f"{os.getpid()} {old}")
    RunLock(settings.lock_file).acquire()
    assert settings.lock_file.read_text().split()[0] == str(os.getpid())
    with pytest.raises(LockHeldError):  # a fresh lock still blocks
        RunLock(settings.lock_file).acquire()


def test_old_lock_without_a_time_falls_back_to_file_age(settings: Settings) -> None:
    settings.lock_file.parent.mkdir(parents=True)
    settings.lock_file.write_text(str(os.getpid()))
    old = time.time() - STALE_AFTER_S - 60
    os.utime(settings.lock_file, (old, old))
    RunLock(settings.lock_file).acquire()


def test_racing_lock_create_is_a_held_lock_not_a_crash(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def racing_open(*_: object, **__: object) -> int:
        raise FileExistsError("taken between check and create")

    monkeypatch.setattr("jobagent.lock.os.open", racing_open)
    with pytest.raises(LockHeldError):
        RunLock(settings.lock_file).acquire()


# --- kill switch kept in the database (R12) ---


def test_stop_check_is_read_and_cached(policy: Policy, clock: FakeClock) -> None:
    calls: list[int] = []
    flag = [False]

    def check() -> bool:
        calls.append(1)
        return flag[0]

    policy.stop_check = check
    assert not policy.stopped()
    flag[0] = True
    assert not policy.stopped() and len(calls) == 1  # cached for a few seconds
    clock.now += 5
    assert policy.stopped() and len(calls) == 2
    assert not policy.begin("greenhouse", "discover").allowed


def flaky_check(*answers: bool | None) -> tuple[Any, list[int]]:
    """A stop check giving `answers` in turn; None is a failed DB read."""
    calls: list[int] = []

    def check() -> bool:
        calls.append(1)
        answer = answers[min(len(calls), len(answers)) - 1]
        if answer is None:
            raise ConnectionError("turso busy")
        return answer

    return check, calls


def test_unreadable_kill_switch_lets_discovery_run_but_never_an_apply(
    policy: Policy, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    policy.stop_check, calls = flaky_check(None)
    with caplog.at_level("ERROR", logger="jobagent.policy"):
        assert policy.authorize("greenhouse", "discover").allowed
    assert len(calls) == 3 and clock.slept == [0.5, 0.5]  # three tries, short waits
    assert policy.stop_unread and "turso busy" in caplog.text
    assert policy.authorize("greenhouse", "apply").reason == "kill switch is on"
    clock.now += 5
    policy.authorize("greenhouse", "discover")
    assert len(calls) == 4  # the next check tries once more


def test_kill_switch_read_on_a_retry_is_used(policy: Policy) -> None:
    policy.stop_check, calls = flaky_check(None, True)
    assert not policy.authorize("greenhouse", "discover").allowed
    assert len(calls) == 2 and not policy.stop_unread


def test_failed_read_keeps_the_last_known_kill_switch(policy: Policy, clock: FakeClock) -> None:
    policy.stop_check, calls = flaky_check(True, None, None, False)
    assert policy.stopped("discover")
    clock.now += 5
    assert policy.stopped("discover") and len(calls) == 2  # one try, then the cached ON
    clock.now += 5
    assert policy.stopped("discover") and len(calls) == 3  # retried on the next check
    clock.now += 5
    assert not policy.stopped("discover") and not policy.stop_unread


def test_kill_switch_set_by_cli_is_seen_by_policy(
    settings: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr("jobagent.cli.load_settings", lambda: settings)

    def fresh_policy() -> Policy:
        return Policy.load(
            REPO_ROOT / "policy.yaml", settings.stop_file, clock=clock,
            stop_check=kill_switch_check(settings.db_path),
        )  # fmt: skip

    assert cli.main(["stop"]) == 0
    assert "kill switch: ON" in capsys.readouterr().out
    assert not settings.stop_file.exists()  # the flag lives in the database, not /tmp
    assert fresh_policy().stopped()
    assert cli.main(["status"]) == 0 and "kill switch: ON" in capsys.readouterr().out
    assert cli.main(["resume"]) == 0 and "kill switch: off" in capsys.readouterr().out
    assert not fresh_policy().stopped()


def test_stop_file_still_works_as_a_local_trigger(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr("jobagent.cli.load_settings", lambda: settings)
    engage_kill_switch(settings.stop_file)
    assert cli.main(["status"]) == 0 and "kill switch: ON" in capsys.readouterr().out
    assert cli.main(["resume"]) == 0 and "kill switch: off" in capsys.readouterr().out
    assert not settings.stop_file.exists()
