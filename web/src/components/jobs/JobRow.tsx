import Link from "next/link";
import type { ReactNode } from "react";
import { istDate, type Job } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Tag, buttonClass } from "@/components/ui";
import { ROLE_LABELS, SOURCE_LABELS, WORK_MODE_LABELS, yearsTag } from "./filters";

export type Mark = (job: Job, next: string) => void;

const TODAY = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());

export const isNew = (j: Job) => j.seen_date?.slice(0, 10) === TODAY;

export const coachHref = (j: Job) =>
  `/coach?url=${encodeURIComponent(j.url ?? "")}&title=${encodeURIComponent(j.title)}&company=${encodeURIComponent(j.company)}`;

export function scoreTone(score: number | null) {
  if (score === null) return "text-ink-3";
  return score >= 70 ? "text-up" : score >= 40 ? "text-ink" : "text-ink-3";
}

function Score({ score, big = false }: { score: number | null; big?: boolean }) {
  return (
    <span
      className={`figure font-extrabold ${big ? "text-5xl" : "text-xl"} ${scoreTone(score)}`}
      title={score === null ? "Not scored yet" : "Match score out of 100"}
    >
      {score ?? "–"}
    </span>
  );
}

/** Next moves for a job in this list; marking it moves it to another list. */
function moves(status: string) {
  return [
    status !== "applied" && { next: "applied", label: "Applied", key: "A", icon: "check" as const },
    status !== "dropped" && { next: "dropped", label: "Skip", key: "S", icon: "x" as const },
    status !== "found" && { next: "found", label: "Back to list", key: "", icon: "undo" as const },
  ].filter((m) => m !== false);
}

type RowProps = {
  job: Job;
  status: string;
  selected: boolean;
  expanded: boolean;
  onSelect: (job: Job) => void;
  onMark: Mark;
};

export function JobRow({ job: j, status, selected, expanded, onSelect, onMark }: RowProps) {
  const where = [j.company, j.city || j.location, WORK_MODE_LABELS[j.work_mode] && j.work_mode !== "unknown" ? WORK_MODE_LABELS[j.work_mode] : ""]
    .filter(Boolean)
    .join(" · ");
  const years = yearsTag(j.min_years, j.max_years);
  return (
    <li
      id={`job-${j.id}`}
      data-selected={selected}
      className={`row group relative border-b border-line last:border-b-0 ${isNew(j) ? "tick-in" : ""} ${
        selected ? "bg-sunk" : "hover:bg-sunk/60"
      }`}
    >
      <button
        type="button"
        aria-current={selected ? "true" : undefined}
        aria-expanded={expanded}
        onClick={() => onSelect(j)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left focus-visible:-outline-offset-2"
      >
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate font-bold tracking-tight">{j.title}</span>
            {isNew(j) && <Tag tone="brand">New</Tag>}
          </span>
          <span className="mt-0.5 block truncate text-sm text-ink-2">{where}</span>
          <span className="mt-1 flex items-center gap-2 text-xs text-ink-3">
            <span>{SOURCE_LABELS[j.source] ?? j.source}</span>
            <span aria-hidden="true">·</span>
            <span>{istDate(j.posted_date ?? j.seen_date)}</span>
            {years && (
              <>
                <span aria-hidden="true">·</span>
                <span>{years}</span>
              </>
            )}
          </span>
        </span>
        <span className="flex w-14 shrink-0 flex-col items-end">
          <Score score={j.score} />
          <span className="text-[11px] font-semibold uppercase tracking-wide text-ink-3">match</span>
        </span>
      </button>
      <div className="row-keys absolute top-1/2 right-20 flex -translate-y-1/2 gap-1 max-lg:hidden">
        {moves(status).map((m) => (
          <button
            key={m.next}
            type="button"
            onClick={() => onMark(j, m.next)}
            title={m.key ? `${m.label} (${m.key})` : m.label}
            className={`flex size-8 items-center justify-center rounded-ctl border border-line-strong bg-panel text-ink-2 shadow-sm hover:text-ink ${
              m.next === "applied" ? "hover:border-up hover:text-up" : m.next === "dropped" ? "hover:border-down hover:text-down" : ""
            }`}
          >
            <Icon name={m.icon} />
            <span className="sr-only">
              {m.label}: {j.title}
            </span>
          </button>
        ))}
        <Link
          href={coachHref(j)}
          title="Coach me (C)"
          className="flex size-8 items-center justify-center rounded-ctl border border-line-strong bg-panel text-ink-2 shadow-sm hover:border-brand hover:text-brand"
        >
          <Icon name="chat" />
          <span className="sr-only">Coach me: {j.title}</span>
        </Link>
      </div>
      {expanded && (
        <div className="border-t border-line bg-panel px-4 pb-4 lg:hidden">
          <JobDetail job={j} status={status} onMark={onMark} />
        </div>
      )}
    </li>
  );
}

const YEAR_SCALE = 10;

/** Years asked as an exact range on a 0-10 ruler, so "2-5" and "5+" read at a glance. */
function YearsRuler({ min, max }: { min: number | null; max: number | null }) {
  if (min === null && max === null) return <span className="text-ink-3">Not stated</span>;
  const lo = Math.min(min ?? 0, YEAR_SCALE);
  const hi = Math.min(max ?? YEAR_SCALE, YEAR_SCALE);
  const open = max === null;
  return (
    <span className="flex flex-col gap-1.5">
      <span className="font-semibold">{yearsTag(min, max)}</span>
      <span aria-hidden="true" className="relative block h-1.5 w-full max-w-56 rounded-full bg-sunk">
        <span
          className={`absolute inset-y-0 rounded-full ${lo <= 2 ? "bg-up" : lo <= 4 ? "bg-brand" : "bg-warn"} ${open ? "opacity-70" : ""}`}
          style={{ left: `${(lo / YEAR_SCALE) * 100}%`, width: `${Math.max(((hi - lo) / YEAR_SCALE) * 100, 3)}%` }}
        />
      </span>
      <span aria-hidden="true" className="figure flex max-w-56 justify-between text-[10px] text-ink-3">
        <span>0</span>
        <span>5</span>
        <span>10+</span>
      </span>
    </span>
  );
}

export function JobDetail({ job: j, status, onMark }: { job: Job; status: string; onMark: Mark }) {
  const facts: [string, ReactNode][] = [
    ["Years asked", <YearsRuler key="y" min={j.min_years} max={j.max_years} />],
    ["Location", j.location || "Not stated"],
    ["Work mode", WORK_MODE_LABELS[j.work_mode] ?? "Not stated"],
    ["Role", ROLE_LABELS[j.role] ?? "Other"],
    ["Job site", SOURCE_LABELS[j.source] ?? j.source],
    ["Posted", istDate(j.posted_date ?? j.seen_date)],
  ];
  if (j.experience_text) facts.splice(1, 0, ["Experience", j.experience_text]);
  if (j.drop_reason) facts.push(["Left out because", j.drop_reason.replaceAll("_", " ")]);
  return (
    <article className="pane-in flex flex-col gap-5 pt-4" key={j.id}>
      <header>
        <h2 className="text-xl leading-tight font-extrabold tracking-tight text-balance">{j.title}</h2>
        <p className="mt-1 text-ink-2">{j.company}</p>
      </header>
      <div className="flex items-end gap-4 border-y border-line py-4">
        <span className="flex flex-col">
          <span className="text-[11px] font-bold uppercase tracking-wide text-ink-3">Match</span>
          <Score score={j.score} big />
        </span>
        <p className="pb-1 text-sm text-ink-2">
          {j.rank_reason ?? (j.score === null ? "Not scored yet. Use Best match sort after the next scan." : "")}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Link href={coachHref(j)} className={buttonClass("primary")}>
          <Icon name="chat" />
          Coach me
        </Link>
        {moves(status).map((m) => (
          <button key={m.next} type="button" className={buttonClass("ghost")} onClick={() => onMark(j, m.next)}>
            <Icon name={m.icon} className={m.next === "applied" ? "text-up" : m.next === "dropped" ? "text-down" : ""} />
            {m.label}
          </button>
        ))}
        {j.url && (
          <a href={j.url} target="_blank" rel="noreferrer" className={buttonClass("quiet")}>
            Open posting
            <Icon name="external" />
          </a>
        )}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
        {facts.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-ink-3">{k}</dt>
            <dd className="min-w-0 break-words">{v}</dd>
          </div>
        ))}
      </dl>
      <p className="hidden text-xs text-ink-3 lg:block">
        Keys: <kbd className="font-semibold text-ink-2">J</kbd> / <kbd className="font-semibold text-ink-2">K</kbd> move ·{" "}
        <kbd className="font-semibold text-ink-2">A</kbd> applied · <kbd className="font-semibold text-ink-2">S</kbd> skip ·{" "}
        <kbd className="font-semibold text-ink-2">C</kbd> coach · <kbd className="font-semibold text-ink-2">O</kbd> open posting
      </p>
    </article>
  );
}
