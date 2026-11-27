"""Discovery precision eval (stage 4): which NEW jobs each filter lets through and drops,
scored against on-target / off-target labels on a frozen snapshot of one daily run.

The snapshot is the NEW found jobs of one run read from Turso (full text), frozen to a
JSON file outside the repo so the eval does not change as the boards change. Labels are a
CSV with `id`, `label` (yes = on-target, no = off-target) and `sure`. Both live outside the
repo, which is public. No model calls and no Turso: it refuses to run while
TURSO_DATABASE_URL is set.

Every filter goes through `rules.check_hard_rules`, the function the save step uses, with
the stage 4 `RuleFilters` switched on one at a time, then together. Per filter and source:
  off_kept    off-target jobs still let through (lower is better)
  off_rate    off_kept / jobs let through
  on_dropped  on-target jobs the filter drops (must stay 0)
`--heldout` runs the same filters over another labelled pool (the stage 2 ranking pool,
which the rules were not tuned on) and lists any on-target job they would drop.

Usage:
  FASTEMBED_CACHE_PATH=~/job-jarvis-labels/.fastembed uv run python -m evals.run_discovery_eval \
    --snapshot ~/job-jarvis-labels/stage4/new-jobs-run53.json \
    --labels ~/job-jarvis-labels/stage4/labels-run53.csv \
    --heldout ~/job-jarvis-labels/label-pool-2026-11-27 \
    --report ~/job-jarvis-labels/stage4/report.json [--history] [--no-embed] [--margin 0.05]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jobagent.index import Embedder, load_embedder
from jobagent.models import Posting
from jobagent.rules import RuleFilters, check_hard_rules, years_asked
from jobagent.targeting import DEFAULT_MARGIN, TitleFilter

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "discovery_history.csv"
IST = timezone(timedelta(hours=5, minutes=30))
COLUMNS = (
    "date_ist", "snapshot", "labels", "filters", "margin", "jobs", "on_target", "off_target",
    "kept", "off_kept", "off_rate", "on_dropped", "heldout_on_dropped", "heldout_off_dropped",
)  # fmt: skip


def posting_of(row: dict[str, Any]) -> Posting:
    return Posting.model_construct(
        source=row["source"], url=row.get("url") or "", company=row.get("company") or "",
        title=row["title"], location=row.get("location") or "",
        experience_text=row.get("experience_text") or "", description=row.get("description") or "",
        closed=False,
    )  # fmt: skip


def read_labels(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="") as fh:
        return {str(r.get("id") or r.get("job_id")): r for r in csv.DictReader(fh)}


@dataclass(frozen=True)
class Config:
    name: str
    filters: RuleFilters | None
    years_4: bool = False  # info only: the stage 3 years decision, not a stage 4 filter


def drop_of(cfg: Config, row: dict[str, Any]) -> str | None:
    posting = posting_of(row)
    reason: str | None = check_hard_rules(posting, filters=cfg.filters)
    if reason is None and cfg.years_4:
        asked = years_asked(posting)
        reason = "years_4_plus" if asked and asked[0] >= 4 else None
    return reason


def configs(make_embedder: Callable[[], Embedder] | None, margin: float) -> list[Config]:
    kw = TitleFilter(None)
    emb = TitleFilter(make_embedder, margin) if make_embedder else None
    title = emb or kw
    every = RuleFilters(title=title.off_target, region_words=True, social_location=True)
    out = [
        Config("baseline (rules v5)", None),
        Config("region_words", RuleFilters(region_words=True)),
        Config("social_location", RuleFilters(social_location=True)),
        Config("title_filter: keywords", RuleFilters(title=kw.off_target)),
    ]
    if emb:
        out.append(Config(f"title_filter: keywords + embedding (margin {margin})",
                          RuleFilters(title=emb.off_target)))  # fmt: skip
    out += [
        Config("all three", every),
        Config("info: all three + years 4+ (stage 3 decision)", every, years_4=True),
    ]
    return out


def score(
    cfg: Config, jobs: list[dict[str, Any]], labels: dict[str, dict[str, str]]
) -> dict[str, Any]:
    per: dict[str, dict[str, int]] = {}
    drops, unlabelled = [], []
    for row in jobs:
        lab = labels.get(str(row["id"]))
        reason = drop_of(cfg, row)
        if lab is None:
            if reason:
                unlabelled.append(_brief(row, reason, None))
            continue
        on = lab["label"].strip().lower() == "yes"
        s = per.setdefault(row["source"], dict.fromkeys(
            ("jobs", "on", "off", "kept", "off_kept", "on_dropped", "off_dropped"), 0))  # fmt: skip
        s["jobs"] += 1
        s["on" if on else "off"] += 1
        if reason:
            s["on_dropped" if on else "off_dropped"] += 1
            drops.append(_brief(row, reason, lab))
        else:
            s["kept"] += 1
            s["off_kept"] += not on
    total = {k: sum(s[k] for s in per.values()) for k in next(iter(per.values()), {})}
    return {"per_source": per, "total": total, "drops": drops, "unlabelled_drops": unlabelled}


def _brief(row: dict[str, Any], reason: str, lab: dict[str, str] | None) -> dict[str, Any]:
    out = {"id": row["id"], "source": row["source"], "title": row["title"],
           "location": row.get("location") or "", "reason": reason}  # fmt: skip
    if lab:
        out |= {"label": lab["label"], "sure": lab.get("sure", "")}
    return out


def rate(t: dict[str, int]) -> float:
    return t["off_kept"] / t["kept"] if t.get("kept") else 0.0


def heldout(cfg: Config, pool: Path) -> dict[str, Any]:
    jobs = json.loads(pool.with_suffix(".json").read_text())["jobs"]
    labels = read_labels(pool.with_suffix(".csv"))
    for lab in labels.values():
        lab["id"] = lab["job_id"]
    return score(cfg, jobs, {lab["job_id"]: lab for lab in labels.values()})


def _per_source(report: dict[str, Any]) -> None:
    """Off-target let through / labelled jobs per source and filter; "!n" = on-target dropped."""
    print("\nper source: off-target let through / jobs (!n = on-target dropped)")
    names = list(report)
    first = report[names[0]]["per_source"]
    print(f"{'source':16}" + "".join(f" {n[:14]:>15}" for n in names))
    for src in sorted(first, key=lambda s: -first[s]["jobs"]):
        cells = []
        for n in names:
            s = report[n]["per_source"][src]
            bad = f" !{s['on_dropped']}" if s["on_dropped"] else ""
            cells.append(f"{s['off_kept']}/{s['jobs']}{bad}")
        print(f"{src:16}" + "".join(f" {c:>15}" for c in cells))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--heldout", type=Path, help="stage 2 pool path, without .csv/.json")
    parser.add_argument("--report", type=Path, help="write per-filter drop lists here (JSON)")
    parser.add_argument("--history", action="store_true", help="append rows to " + HISTORY.name)
    parser.add_argument("--no-embed", action="store_true", help="keywords only")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    args = parser.parse_args(argv)
    if os.environ.get("TURSO_DATABASE_URL"):
        parser.error("unset TURSO_DATABASE_URL: this eval reads only the frozen snapshot")
    snap = json.loads(args.snapshot.expanduser().read_text())
    jobs = [j for j in snap["jobs"] if j["status"] == "found"]
    labels = read_labels(args.labels.expanduser())
    model = None if args.no_embed else load_embedder()

    def make() -> Embedder:
        assert model is not None
        return model

    report: dict[str, Any] = {}
    rows = []
    print(f"{len(jobs)} NEW found jobs in the snapshot, {len(labels)} labelled (Claude's labels)")
    print(f"{'filter':52} {'kept':>5} {'off_kept':>8} {'off_rate':>8} {'on_drop':>7}"
          f" {'held on_drop':>12} {'held off_drop':>13}")  # fmt: skip
    for cfg in configs(make if model else None, args.margin):
        got = score(cfg, jobs, labels)
        held = heldout(cfg, args.heldout.expanduser()) if args.heldout else None
        t, h = got["total"], (held["total"] if held else {})
        got["heldout"] = held
        report[cfg.name] = got
        print(f"{cfg.name:52} {t['kept']:5} {t['off_kept']:8} {rate(t):8.2f} {t['on_dropped']:7}"
              f" {h.get('on_dropped', ''):>12} {h.get('off_dropped', ''):>13}")  # fmt: skip
        embeds = model is not None and cfg.filters is not None and cfg.filters.title is not None
        rows.append({
            "date_ist": datetime.now(IST).strftime("%d-%m-%Y %H:%M IST"),
            "snapshot": args.snapshot.name, "labels": f"{t['jobs']} ({t['on']} on-target, Claude)",
            "filters": cfg.name, "margin": args.margin if embeds and "keywords" not in cfg.name
            or embeds and "embedding" in cfg.name else "",
            "jobs": t["jobs"], "on_target": t["on"], "off_target": t["off"], "kept": t["kept"],
            "off_kept": t["off_kept"], "off_rate": round(rate(t), 3),
            "on_dropped": t["on_dropped"], "heldout_on_dropped": h.get("on_dropped", ""),
            "heldout_off_dropped": h.get("off_dropped", ""),
        })  # fmt: skip
    _per_source(report)
    if args.report:
        args.report.expanduser().write_text(json.dumps(report, indent=1, ensure_ascii=False))
        print("\ndrop lists:", args.report)
    if args.history:
        new = not HISTORY.exists()
        with HISTORY.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
