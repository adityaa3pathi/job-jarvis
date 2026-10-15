"use client";

import { SOURCE_LABELS } from "./filters";

const ORDER = Object.keys(SOURCE_LABELS);

type Props = { sources: Record<string, number>; active: string; onSelect: (source: string) => void };

/** "All" plus one tab per job site that has jobs under the current filters. */
export function SourceTabs({ sources, active, onSelect }: Props) {
  const names = Object.keys(sources).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  if (active && !names.includes(active)) names.push(active);
  const all = Object.values(sources).reduce((sum, n) => sum + n, 0);
  return (
    <div role="group" aria-label="Job site" className="flex flex-wrap gap-1.5">
      <Tab label="All" count={all} on={active === ""} onClick={() => onSelect("")} />
      {names.map((s) => (
        <Tab key={s} label={SOURCE_LABELS[s] ?? s} count={sources[s] ?? 0} on={active === s} onClick={() => onSelect(s)} />
      ))}
    </div>
  );
}

function rank(source: string) {
  const i = ORDER.indexOf(source);
  return i === -1 ? ORDER.length : i;
}

function Tab({ label, count, on, onClick }: { label: string; count: number; on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-sm ${
        on ? "border-zinc-900 bg-zinc-900 text-white" : "border-zinc-300 bg-white text-zinc-800 hover:bg-zinc-100"
      }`}
    >
      {label} <span className={on ? "text-zinc-300" : "text-zinc-500"}>{count.toLocaleString("en-IN")}</span>
    </button>
  );
}
