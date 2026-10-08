"""Command line entry point. Only status and the kill switch exist so far."""

from __future__ import annotations

import argparse
import sys

from jobagent.config import load_settings
from jobagent.policy import clear_kill_switch, engage_kill_switch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stop", "resume"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.command == "stop":
        engage_kill_switch(settings.stop_file)
    elif args.command == "resume":
        clear_kill_switch(settings.stop_file)
    state = "ON (agent halted)" if settings.stop_file.exists() else "off"
    sys.stdout.write(f"kill switch: {state}\nstop file: {settings.stop_file}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
