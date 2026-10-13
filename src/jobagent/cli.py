"""Command line entry point: status, the kill switch, discovery and the resume tailor."""

from __future__ import annotations

import argparse
import sys

from jobagent.config import REPO_ROOT, Settings, ensure_data_dir, load_settings
from jobagent.db import Database
from jobagent.discovery import discover_all
from jobagent.discovery.base import load_companies, load_searches
from jobagent.discovery.fetch import HttpFetcher
from jobagent.policy import Policy, clear_kill_switch, engage_kill_switch
from jobagent.resume import cli_tailor


def discover(settings: Settings) -> int:
    """Read every enabled company board and job-board search, then store new postings."""
    policy = Policy.load(REPO_ROOT / "policy.yaml", settings.stop_file)
    result = discover_all(
        load_companies(REPO_ROOT / "companies.yaml"),
        HttpFetcher(),
        policy,
        load_searches(REPO_ROOT / "searches.yaml"),
    )
    ensure_data_dir(settings)
    db = Database(settings.db_path, settings.max_years_asked)
    out = sys.stdout
    try:
        for p in result.postings:
            saved = db.upsert_posting(p)
            if saved.outcome == "new" and saved.status == "found":
                out.write(f"NEW  {p.source:9} {p.title} | {p.company} | {p.location} | {p.url}\n")
    finally:
        db.close()
    for u in result.unreachable:
        out.write(f"UNREACHABLE  {u.company}: {u.reason}\n")
    for line in result.skipped_by_policy:
        out.write(f"SKIPPED  {line}\n")
    out.write(f"read {len(result.postings)} postings\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stop", "resume", "discover", "hash-password"):
        sub.add_parser(name)
    cli_tailor.add_arguments(sub.add_parser("tailor"))
    args = parser.parse_args(argv)
    if args.command == "tailor":
        return cli_tailor.run(args)
    if args.command == "hash-password":
        from getpass import getpass

        from jobagent.web.auth import hash_password

        sys.stdout.write(hash_password(getpass("Web login password: ")) + "\n")
        return 0
    settings = load_settings()
    if args.command == "discover":
        return discover(settings)
    if args.command == "stop":
        engage_kill_switch(settings.stop_file)
    elif args.command == "resume":
        clear_kill_switch(settings.stop_file)
    state = "ON (agent halted)" if settings.stop_file.exists() else "off"
    sys.stdout.write(f"kill switch: {state}\nstop file: {settings.stop_file}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
