"""Backup to gzipped SQL and restore into a fresh file (U9, R12)."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path
from typing import Any

import libsql
import pytest

from jobagent import backup as backup_module
from jobagent import cli
from jobagent.backup import backup, dump_sql, restore
from jobagent.config import Settings
from jobagent.db import Database
from jobagent.llm import Usage
from jobagent.models import Posting
from jobagent.sqlconn import LibsqlConnection
from jobagent.web.store import Store

TABLES = (
    "jobs", "job_links", "events", "profile", "flags", "runs", "embeddings", "labels",
    "digests", "digest_jobs", "fetch_cache", "llm_cache", "llm_usage", "trace",
)  # fmt: skip


def _fill(path: Path) -> None:
    db = Database(path)
    saved = db.upsert_posting(
        Posting(source="hirist", url="https://www.hirist.tech/j/a-1", company="O'Brien & Co",
                title="Full Stack Developer", location="Pune", description="line 1\nline 2;\n")
    )  # fmt: skip
    db.upsert_posting(Posting(source="lever", url="https://jobs.lever.co/x/1", company="Lever Co",
                              title="Backend Engineer", location="Remote"))  # fmt: skip
    db.set_status(saved.job_id, "applied", "owner")
    db.mark(saved.job_id, "interview")  # writes a label
    db.save_vectors("m", {"k": bytes(range(256)) + b"\x00'\n"})
    db.save_validators({"https://x.example/list": ('W/"1"', None, "abc")})
    db.claim_digest(1)
    db.finish_digest(1, [saved.job_id])
    db.close()
    st = Store(path)
    summary = "Résumé: 'quoted' \"text\"\n-- not a comment\r\nend"
    st.save_profile(summary=summary, base_tex="\\section{x}")
    st.set_kill_switch(True)
    st.finish_run(st.start_run(), "done", "", {"saved": 2, "notes": ["n"]})
    st.cache_put("key", "step", '{"a": "b\'c"}')
    st.add_usage(Usage("step", "model", 1, 2, 0, 3, output="out\r\nput"))
    st.close()


def _rows(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}
    finally:
        conn.close()


def _cli(settings: Settings, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("jobagent.cli.load_settings", lambda: settings)
    return cli.main(list(argv))


def test_backup_then_restore_gives_identical_rows(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill(settings.db_path)
    before = _rows(settings.db_path)
    assert all(before[t] for t in TABLES)
    out, new = tmp_path / "b.sql.gz", tmp_path / "new.sqlite3"
    assert _cli(settings, monkeypatch, "backup", str(out)) == 0
    assert gzip.decompress(out.read_bytes()).startswith(b"BEGIN TRANSACTION;")
    assert _cli(settings, monkeypatch, "restore", str(out), str(new)) == 0
    assert _rows(new) == before
    conn = sqlite3.connect(new)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert {"jobs_key", "events_job"} <= names


def test_restore_refuses_an_existing_target(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _fill(settings.db_path)
    out, target = tmp_path / "b.sql.gz", tmp_path / "taken.sqlite3"
    assert _cli(settings, monkeypatch, "backup", str(out)) == 0
    target.write_bytes(b"keep me")
    assert _cli(settings, monkeypatch, "restore", str(out), str(target)) == 1
    assert target.read_bytes() == b"keep me"
    assert "exists" in capsys.readouterr().out


def test_dump_works_over_a_libsql_connection(tmp_path: Path) -> None:
    _fill(tmp_path / "src.sqlite3")
    conn = LibsqlConnection(libsql.connect(str(tmp_path / "src.sqlite3")))
    script = "".join(dump_sql(conn))
    conn.close()
    new = sqlite3.connect(tmp_path / "new.sqlite3")
    new.executescript(script)
    new.close()
    assert _rows(tmp_path / "new.sqlite3") == _rows(tmp_path / "src.sqlite3")


def test_files_are_private_and_a_cut_short_dump_leaves_nothing(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill(settings.db_path)
    out, new = tmp_path / "b.sql.gz", tmp_path / "new.sqlite3"
    assert _cli(settings, monkeypatch, "backup", str(out)) == 0
    assert _cli(settings, monkeypatch, "restore", str(out), str(new)) == 0
    assert (out.stat().st_mode & 0o777, new.stat().st_mode & 0o777) == (0o600, 0o600)
    text = gzip.decompress(out.read_bytes()).decode()
    cut = tmp_path / "cut.sql.gz"
    cut.write_bytes(gzip.compress(text.removesuffix("COMMIT;\n").encode()))
    with pytest.raises(ValueError, match="cut short"):
        restore(cut, tmp_path / "partial.sqlite3")
    assert not (tmp_path / "partial.sqlite3").exists()


def test_a_failed_dump_leaves_the_old_backup_intact(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill(settings.db_path)
    out = tmp_path / "b.sql.gz"
    assert _cli(settings, monkeypatch, "backup", str(out)) == 0
    good = out.read_bytes()

    def broken(conn: Any) -> Any:
        yield "BEGIN TRANSACTION;\n"
        raise OSError("turso dropped the stream")

    monkeypatch.setattr(backup_module, "dump_sql", broken)
    conn = sqlite3.connect(settings.db_path)
    with pytest.raises(OSError, match="dropped"):
        backup(conn, out)
    conn.close()
    assert out.read_bytes() == good
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("b.")) == [out.name]
