from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from jobagent.audit import AuditLog
from jobagent.redact import Redactor, configure_logging

TOKEN = "123456789:AAH-secret_token_value_abcdefghijk"


def make_log(tmp_path: Path) -> AuditLog:
    redactor = Redactor(["supersecretvalue99"])
    return AuditLog(tmp_path / "audit", redactor)


def test_entry_has_fields_and_mode(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    entry = log.write("greenhouse", "job-1", "fill", "dry_run")
    assert {"time", "portal", "job", "action", "result"} <= entry.keys()
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    assert len(log.entries()) == 1


def test_secrets_never_reach_the_audit_log(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.write(
        "lever",
        "j",
        "fetch",
        "ok",
        detail=(
            f"env supersecretvalue99 token bot{TOKEN} key AIza{'a' * 35} "
            "sk-abcdefghijklmnopqrstuv url https://x.example/p?token=abc&id=5"
        ),
    )
    text = log.path.read_text()
    for leaked in ("supersecretvalue99", TOKEN, "AIzaaaaa", "sk-abcdef", "token=abc", "id=5"):
        assert leaked not in text
    assert "https://x.example/p" in text


def test_exception_text_is_redacted(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.error("lever", "j", "fetch", RuntimeError("failed with supersecretvalue99"))
    assert "supersecretvalue99" not in log.path.read_text()


def test_logging_output_is_redacted(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    configure_logging(Redactor(["supersecretvalue99"]))
    logger = logging.getLogger("jobagent.test")
    try:
        raise ValueError("boom supersecretvalue99")
    except ValueError:
        logger.exception("call to https://api.example/x?key=zzz failed bot%s", TOKEN)
    err = capsys.readouterr().err
    assert "supersecretvalue99" not in err
    assert TOKEN not in err
    assert "key=zzz" not in err
    assert logging.getLogger("httpx").level == logging.WARNING


def test_redactor_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "envvalue-123456")
    assert "envvalue-123456" not in Redactor.from_environment()("x envvalue-123456 y")


def test_screenshot_saved_private(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    shot = log.save_screenshot(b"\x89PNG", "greenhouse/job 1")
    assert shot.parent == log.directory
    assert stat.S_IMODE(shot.stat().st_mode) == 0o600
