"""Command line entry point: status, the kill switch, discovery and the resume tailor."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from jobagent.config import REPO_ROOT, Settings, ensure_data_dir, load_settings
from jobagent.db import Database
from jobagent.digest import digest_last_run, sender_from_env
from jobagent.discovery import discover_all
from jobagent.discovery.base import DiscoveryResult, load_companies, load_searches
from jobagent.discovery.fetch import HttpFetcher
from jobagent.index import shared_embedder
from jobagent.llm import LLM, run_scope
from jobagent.lock import LockHeldError, RunLock
from jobagent.policy import Policy, clear_kill_switch
from jobagent.resume import cli_tailor
from jobagent.runs import (
    RunTally,
    digest_after_run,
    kill_switch_check,
    prune_after_run,
    rank_after_run,
    rules_swept,
    save_portal,
    sweep_context,
)
from jobagent.web.store import Store


def discover(settings: Settings) -> int:
    """Read every enabled company board and job-board search, saving each portal as it ends."""
    out, lock = sys.stdout, RunLock(settings.lock_file)
    try:
        lock.acquire()
    except LockHeldError as exc:
        out.write(f"not started: {exc}\n")
        return 1
    try:
        ensure_data_dir(settings)
        db, st = Database(settings.db_path, settings.max_years_asked), Store(settings.db_path)
        policy = Policy.load(
            REPO_ROOT / "policy.yaml", settings.stop_file,
            stop_check=kill_switch_check(settings.db_path),
        )  # fmt: skip
        tally, run_id = RunTally(), st.start_run()

        def save(part: DiscoveryResult) -> None:
            saved = save_portal(
                part, db=db, st=st, run_id=run_id, tally=tally,
                after_runs=settings.close_after_clean_runs,
            )  # fmt: skip
            for p, r in zip(part.postings, saved or [], strict=False):
                if r.outcome == "new" and r.status == "found":
                    out.write(
                        f"NEW  {p.source:9} {p.title} | {p.company} | {p.location} | {p.url}\n"
                    )

        try:
            discover_all(
                load_companies(REPO_ROOT / "companies.yaml"), HttpFetcher(), policy,
                load_searches(REPO_ROOT / "searches.yaml"), on_portal_done=save,
                ctx=sweep_context(db, settings.detail_fetch_cap),
            )  # fmt: skip
            rules_swept(db, tally)
            prune_after_run(db, tally)
            rank_after_run(
                db, st, tally, embedder=shared_embedder(), llm=judge_llm(settings),
                settings=settings,
            )  # fmt: skip
        except Exception as exc:
            st.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}", tally.as_dict())
            raise
        else:
            st.finish_run(run_id, "done", "", tally.as_dict())
            sent = digest_after_run(db, run_id, sender_from_env(os.environ), settings)
            out.write(f"DIGEST  {digest_line(sent)}\n")
        finally:
            db.close()
            st.close()
    finally:
        lock.release()
    report(tally.as_dict(), out)
    return 0


def judge_llm(settings: Settings) -> LLM | None:
    """The ranking judge (U14) runs only when an AI key is set."""
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    from jobagent.llm import GeminiLLM

    return GeminiLLM.from_settings(settings.llm, 0.0)


def digest_line(result: dict[str, Any]) -> str:
    if result.get("sent"):
        return f"sent {result['sent']} jobs to Telegram"
    return str(result.get("skipped") or f"not sent: {result.get('error', '?')}")


def digest(settings: Settings) -> int:
    """Send the last finished run's digest to Telegram, once (U15)."""
    ensure_data_dir(settings)
    db = Database(settings.db_path, settings.max_years_asked)
    try:
        result = digest_last_run(
            db, sender_from_env(os.environ), limit=settings.ranking.digest_top_n
        )
    finally:
        db.close()
    sys.stdout.write(f"digest: {digest_line(result)}\n")
    return 1 if "error" in result else 0


def report(counts: dict[str, Any], out: TextIO) -> None:
    for line in counts["unreachable"]:
        out.write(f"UNREACHABLE  {line}\n")
    for line in counts["skipped"]:
        out.write(f"SKIPPED  {line}\n")
    for line in counts["notes"]:
        out.write(f"NOTE  {line}\n")
    if unchanged := counts.get("unchanged"):
        out.write(f"UNCHANGED  {unchanged} boards same as last run, not re-read\n")
    for name, c in counts["portals"].items():
        out.write(f"PORTAL  {name}: read {c['read']}, new {c['new']}, saved {c['saved']}\n")
    if drops := counts["drop_reasons"]:
        out.write("DROPPED  " + ", ".join(f"{k}={v}" for k, v in drops.items()) + "\n")
    out.write(f"read {counts['read']} postings, {counts['new']} new\n")


def kill_switch(settings: Settings, command: str) -> int:
    """The switch is a DB flag so it survives restarts; the STOP file still works locally."""
    ensure_data_dir(settings)
    st = Store(settings.db_path)
    try:
        if command in ("stop", "resume"):
            st.set_kill_switch(command == "stop")
        if command == "resume":
            clear_kill_switch(settings.stop_file)
        on = st.kill_switch() or settings.stop_file.exists()
    finally:
        st.close()
    state = "ON (agent halted)" if on else "off"
    sys.stdout.write(f"kill switch: {state}\nstop file: {settings.stop_file}\n")
    return 0


def recheck(settings: Settings) -> int:
    """Re-run the hard rules and today's dedupe on every stored job (U4, U7)."""
    lock = RunLock(settings.lock_file)
    try:
        lock.acquire()
    except LockHeldError as exc:
        sys.stdout.write(f"not started: {exc}\n")
        return 1
    try:
        ensure_data_dir(settings)
        db = Database(settings.db_path, settings.max_years_asked)
        try:
            s = db.recheck()
        finally:
            db.close()
    finally:
        lock.release()
    sys.stdout.write(
        f"rechecked {s.rechecked}, undropped {s.undropped}, dropped {s.dropped},"
        f" merged {s.merged}\n"
    )
    return 0


def backup(settings: Settings, out: Path) -> int:
    """Dump the configured database (Turso when TURSO_DATABASE_URL is set) to gzipped SQL."""
    from jobagent.backup import backup as dump
    from jobagent.sqlconn import connect

    conn = connect(settings.db_path)
    try:
        lines = dump(conn, out)
    finally:
        conn.close()
    sys.stdout.write(f"backed up {lines} lines to {out} ({out.stat().st_size} bytes)\n")
    return 0


def restore(dump: Path, target: Path) -> int:
    from jobagent.backup import restore as load

    try:
        load(dump, target)
    except FileExistsError as exc:
        sys.stdout.write(f"not restored: {exc}\n")
        return 1
    sys.stdout.write(f"restored {dump} into {target}\n")
    return 0


def prune(settings: Settings) -> int:
    """Blank the text of jobs the rules dropped over 30 days ago (U4)."""
    lock = RunLock(settings.lock_file)
    try:
        lock.acquire()
    except LockHeldError as exc:
        sys.stdout.write(f"not started: {exc}\n")
        return 1
    try:
        ensure_data_dir(settings)
        db = Database(settings.db_path, settings.max_years_asked)
        try:
            pruned = db.prune()
        finally:
            db.close()
    finally:
        lock.release()
    sys.stdout.write(f"pruned text of {pruned} dropped jobs\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "status", "stop", "resume", "discover", "recheck", "prune", "hash-password", "digest",
    ):  # fmt: skip
        sub.add_parser(name)
    cli_tailor.add_arguments(sub.add_parser("tailor"))
    sub.add_parser("backup").add_argument("out", type=Path)
    restore_args = sub.add_parser("restore")
    restore_args.add_argument("dump", type=Path)
    restore_args.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    if args.command == "tailor":
        with run_scope():
            return cli_tailor.run(args)
    if args.command == "restore":
        return restore(args.dump, args.target)
    if args.command == "hash-password":
        from getpass import getpass

        from jobagent.web.auth import hash_password

        sys.stdout.write(hash_password(getpass("Web login password: ")) + "\n")
        return 0
    settings = load_settings()
    if args.command == "discover":
        return discover(settings)
    if args.command == "backup":
        return backup(settings, args.out)
    if args.command == "recheck":
        return recheck(settings)
    if args.command == "prune":
        return prune(settings)
    if args.command == "digest":
        return digest(settings)
    return kill_switch(settings, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
