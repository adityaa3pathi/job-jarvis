"use client";

import { SOURCE_LABELS } from "./filters";

const ORDER = Object.keys(SOURCE_LABELS);

type Props = { sources: Record<string, number>; active: string; onSelect: (source: string) => void };

/** "All" plus one chip per job site that has jobs under the current filters; scrolls sideways on a phone. */
export function SourceTabs({ sources, active, onSelect }: Props) {
  const names = Object.keys(sources).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  if (active && !names.includes(active)) names.push(active);
  const all = Object.values(sources).reduce((sum, n) => sum + n, 0);
  return (
    <div
      role="group"
      aria-label="Job site"
      className="-mx-4 flex gap-1.5 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0"
    >
      <Chip label="All sites" count={all} on={active === ""} onClick={() => onSelect("")} />
      {names.map((s) => (
        <Chip key={s} label={SOURCE_LABELS[s] ?? s} count={sources[s] ?? 0} on={active === s} onClick={() => onSelect(s)} />
      ))}
    </div>
  );
}

function rank(source: string) {
  const i = ORDER.indexOf(source);
  return i === -1 ? ORDER.length : i;
}

function Chip({ label, count, on, onClick }: { label: string; count: number; on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`flex h-7 shrink-0 items-center gap-1.5 rounded-full px-3 text-xs font-semibold whitespace-nowrap transition-colors ${
        on ? "bg-ink text-bg" : "bg-sunk text-ink-2 hover:text-ink"
      }`}
    >
      {label}
      <span className={`figure ${on ? "opacity-70" : "text-ink-3"}`}>{count.toLocaleString("en-IN")}</span>
    </button>
  );
}
