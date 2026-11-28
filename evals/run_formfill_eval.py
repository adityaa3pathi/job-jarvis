"""Form-fill eval: real application forms, an answer key and one recorded model reply per form.

The field lists, the answer key, the saved answers and the recorded replies hold the owner's
details, so they live outside the repo (default ~/job-jarvis-labels). This file only reads them.

    # one live call per form, saved raw; stops at the first 429 or after --max-calls requests
    python -m evals.run_formfill_eval --record --max-calls 30
    # offline: replays the recorded replies through suggest() and check(), no model calls
    python -m evals.run_formfill_eval --out run.json [--set loose_options=true]
    python -m evals.run_formfill_eval --diff before.json after.json

Each field in the key is "fact" (certain from the profile or saved answers), "check" (the
owner's call) or "blank" (the helper must leave it empty). Scores per site:
  correct        right value (fact), a suggestion marked check (check), left empty (blank)
  wrong          a value the key's pattern rejects
  blank_missed   left empty though the key has an answer
  filled_blank   a value where the key says blank
  check_empty    a check field with no key answer left empty (not an error, counted apart)
  wrong_fact     marked fact or saved (Fill all fills it) when wrong or not a fact: must be 0
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from jobagent.config import FormSettings, load_settings
from jobagent.formfill import FormField, FormPage, Suggestion, suggest
from jobagent.llm import GeminiLLM, model_id
from jobagent.web.store import IST

LABELS = Path.home() / "job-jarvis-labels"
PROFILE = Path.home() / "Desktop/career summary files/profile-text-26-11-2026.md"
HISTORY = Path(__file__).with_name("formfill_history.csv")
TRUSTED = {"fact", "saved"}  # what Fill all fills without the owner looking
METRICS = [
    "fields", "correct", "wrong", "blank_missed", "filled_blank", "check_empty", "wrong_fact",
]  # fmt: skip


class StopRecording(Exception):
    """A 429 or the call cap: the run stops instead of retrying or switching keys."""


class CappedTransport(httpx.BaseTransport):
    """Counts every HTTP request (retries and fallback models too) and stops at a 429."""

    def __init__(self, cap: int) -> None:
        self.cap, self.sent = cap, 0
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.sent >= self.cap:
            raise StopRecording(f"call cap of {self.cap} reached")
        self.sent += 1
        response = self._inner.handle_request(request)
        if response.status_code == 429:
            raise StopRecording("HTTP 429: the key is out of quota or rate-limited")
        return response


class Recorder:
    def __init__(self, llm: GeminiLLM) -> None:
        self.llm = llm
        self.last: dict[str, Any] = {}

    @property
    def model(self) -> str:
        return model_id(self.llm)

    def complete_json(self, *, system: str, user: str) -> str:
        started = time.monotonic()
        raw = self.llm.complete_json(system=system, user=user)
        self.last = {"raw": raw, "ms": int((time.monotonic() - started) * 1000),
                     "prompt_sha": _sha(system, user)}  # fmt: skip
        return raw


class Replay:
    """Returns the recorded reply; notes when the prompt differs from the recorded one."""

    model = "replay"

    def __init__(self, record: dict[str, Any]) -> None:
        self.record, self.prompt_changed = record, False

    def complete_json(self, *, system: str, user: str) -> str:
        self.prompt_changed = _sha(system, user) != self.record["prompt_sha"]
        return str(self.record["raw"])


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def load_key(path: Path) -> dict[str, list[dict[str, str]]]:
    forms: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in csv.DictReader(path.open()):
        forms[row["form"]].append(row)
    return dict(forms)


def load_page(
    forms_dir: Path, name: str, rows: list[dict[str, str]], prefs: dict[str, str]
) -> FormPage:
    """The fields the extension would send: contact boxes (filled by the extension itself)
    and boxes the live extension did not read are already out of the key."""
    saved = json.loads((forms_dir / f"{name}.fields.json").read_text())
    wanted = {r["id"] for r in rows}
    fields, frame = [], saved["frames"][0]
    for fr in saved["frames"]:
        for f in fr["fields"]:
            if f["id"] in wanted:
                fields.append(FormField(**{k: v for k, v in f.items() if k != "contact"}))
                frame = fr
    head = (forms_dir / f"{name}.html").read_text(errors="replace")[:400]
    url = re.search(r"saved from url=\(\d+\)(\S+?)\s*-->", head)
    return FormPage(url=url.group(1) if url else "", title=frame["title"][:300],
                    context=frame["context"], fields=fields, prefs=prefs)  # fmt: skip


def grade(row: dict[str, str], s: Suggestion) -> str:
    filled = bool(s.value.strip())
    ok = bool(row["accept"]) and bool(re.search(row["accept"], s.value.strip(), re.I))
    expect = row["expect"]
    if expect == "blank":
        return "filled_blank" if filled else "correct"
    if not filled:
        if expect == "check" and not row["answer"]:
            return "check_empty"
        return "blank_missed"
    return "wrong" if row["accept"] and not ok else "correct"


def score(results: list[dict[str, Any]]) -> dict[str, Counter[str]]:
    sites: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        for c in (sites[r["site"]], sites["TOTAL"]):
            c["fields"] += 1
            c[r["grade"]] += 1
            c["wrong_fact"] += r["wrong_fact"]
    return dict(sites)


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    key = load_key(args.key)
    prefs = {k: str(v) for k, v in (yaml.safe_load(args.prefs.read_text()) or {}).items() if v}
    summary = args.profile.read_text()
    replies = args.labels / "stage5" / "replies"
    replies.mkdir(parents=True, exist_ok=True)
    forms_cfg = FormSettings(**dict(s.split("=", 1) for s in args.set))
    settings = load_settings()
    transport = CappedTransport(args.max_calls)
    results: list[dict[str, Any]] = []
    for name, rows in sorted(key.items()):
        page = load_page(args.labels / "forms", name, rows, prefs)
        path = replies / f"{name}.json"
        if args.record:
            if path.exists():
                print(f"{name}: already recorded, skipped")
                continue
            gemini = GeminiLLM.from_settings(settings.llm, 0.5)  # the web's writer settings
            gemini._client = httpx.Client(timeout=settings.llm.timeout_s, transport=transport)
            rec = Recorder(gemini)
            try:
                suggest(page, summary, [], rec, forms_cfg)
            except StopRecording as exc:
                print(f"{name}: STOPPED, {exc}. {transport.sent} requests sent.")
                sys.exit(2)
            stamp = datetime.now(IST).strftime("%d-%m-%Y %H:%M IST")
            saved = {"name": name, "model": rec.model, "recorded": stamp, **rec.last}
            path.write_text(json.dumps(saved, indent=1))
            path.chmod(0o600)
            print(f"{name}: recorded, {rec.last['ms']} ms, requests so far {transport.sent}")
            continue
        record = json.loads(path.read_text())
        replay = Replay(record)
        found = {s.id: s for s in suggest(page, summary, [], replay, forms_cfg)}
        for row in rows:
            s = found[row["id"]]
            g = grade(row, s)
            wrong_fact = s.status in TRUSTED and (row["expect"] != "fact" or g != "correct")
            results.append({
                "site": row["site"], "form": name, "id": row["id"], "label": row["label"][:90],
                "expect": row["expect"], "grade": g, "wrong_fact": int(wrong_fact),
                "prompt_changed": replay.prompt_changed, **asdict(s),
            })  # fmt: skip
    if args.record:
        print(f"done: {transport.sent} requests")
    return results


def table(results: list[dict[str, Any]]) -> str:
    sites = score(results)
    order = sorted(k for k in sites if k != "TOTAL") + ["TOTAL"]
    lines = ["| site | " + " | ".join(METRICS) + " |", "|---" * (len(METRICS) + 1) + "|"]
    for site in order:
        lines.append(f"| {site} | " + " | ".join(str(sites[site][m]) for m in METRICS) + " |")
    return "\n".join(lines)


def diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    old = {(r["form"], r["id"]): r for r in before}
    for r in after:
        o = old[(r["form"], r["id"])]
        if (o["value"], o["status"]) != (r["value"], r["status"]):
            print(f"- {r['form']} {r['id']} [{r['expect']}] {r['label'][:70]}\n"
                  f"    {o['status']}: {o['value'][:80]!r} ({o['grade']}) -> "
                  f"{r['status']}: {r['value'][:80]!r} ({r['grade']})")  # fmt: skip


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--labels", type=Path, default=LABELS)
    p.add_argument("--key", type=Path, default=LABELS / "stage5" / "answer-key.csv")
    p.add_argument("--prefs", type=Path, default=LABELS / "forms" / "prefs.yaml")
    p.add_argument("--profile", type=Path, default=PROFILE)
    p.add_argument("--record", action="store_true", help="live: one call per form not yet recorded")
    p.add_argument("--max-calls", type=int, default=30, help="cap on HTTP requests when recording")
    p.add_argument("--set", action="append", default=[], help="a FormSettings value, name=value")
    p.add_argument("--out", type=Path, help="write per-field results as JSON")
    p.add_argument("--history", action="store_true", help=f"append a row to {HISTORY.name}")
    p.add_argument("--note", default="")
    p.add_argument("--diff", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = p.parse_args()
    if args.diff:
        diff(*(json.loads(f.read_text()) for f in args.diff))
        return
    results = run(args)
    if args.record:
        return
    if any(r["prompt_changed"] for r in results):
        print("NOTE: the prompt differs from the recorded one for some forms (replies still used)")
    print(table(results))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
        args.out.chmod(0o600)
    if args.history:
        total = score(results)["TOTAL"]
        new = not HISTORY.exists()
        with HISTORY.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["date_ist", "model", "settings", "forms", *METRICS, "note"])
            w.writerow([datetime.now(IST).strftime("%d-%m-%Y %H:%M IST"), "recorded replies",
                        " ".join(args.set) or "defaults", len({r["form"] for r in results}),
                        *(total[m] for m in METRICS), args.note])  # fmt: skip


if __name__ == "__main__":
    main()
