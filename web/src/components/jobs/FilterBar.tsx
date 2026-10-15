"use client";

import { useCallback, useEffect, useId, useState } from "react";
import type { Facet, JobFacets } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, FIELD } from "@/components/ui";
import { DATE_LABELS, MAX_SEARCH, ROLE_LABELS, WORK_MODE_LABELS, YEARS, type Filters } from "./filters";

const SEARCH_DELAY_MS = 300;

type Props = {
  filters: Filters;
  facets: JobFacets | null;
  onChange: (patch: Partial<Filters>) => void;
  onClear: (() => void) | null;
};

export function activeFilterCount(f: Filters): number {
  return [f.role, f.work_mode, f.max_years, f.posted_within_days, f.city, f.company].filter(Boolean).length;
}

/** Filters tucked behind one button, so the list starts right under the search box. */
export function FilterBar({ filters, facets, onChange, onClear }: Props) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const count = activeFilterCount(filters);
  const anyYears = filters.max_years === "";
  const setQ = useCallback((q: string) => onChange({ q }), [onChange]);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-2">
        <SearchBox value={filters.q} onChange={setQ} />
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((o) => !o)}
          className={`flex h-9 shrink-0 items-center gap-1.5 rounded-ctl border px-3 text-sm font-semibold transition-colors ${
            open || count ? "border-brand bg-brand-soft text-brand" : "border-line-strong bg-panel text-ink-2 hover:border-ink-3"
          }`}
        >
          <Icon name="filter" />
          Filters
          {count > 0 && (
            <span className="figure grid size-5 place-items-center rounded-full bg-brand text-[11px] text-brand-ink">{count}</span>
          )}
        </button>
      </div>
      <div id={panelId} hidden={!open} className="rounded-lg border border-line bg-panel p-4">
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
          <Select
            label="Role"
            value={filters.role}
            options={[["", "Any"], ...Object.entries(ROLE_LABELS)]}
            onChange={(role) => onChange({ role })}
          />
          <Select
            label="Work mode"
            value={filters.work_mode}
            options={[["", "Any"], ...Object.entries(WORK_MODE_LABELS)]}
            onChange={(work_mode) => onChange({ work_mode })}
          />
          <Select
            label="Date posted"
            value={filters.posted_within_days}
            options={[["", "Any time"], ...Object.entries(DATE_LABELS)]}
            onChange={(posted_within_days) => onChange({ posted_within_days })}
          />
          <Picker label="City" value={filters.city} options={facets?.cities ?? []} onChange={(city) => onChange({ city })} />
          <Picker
            label="Company"
            value={filters.company}
            options={facets?.companies ?? []}
            onChange={(company) => onChange({ company })}
          />
          <div className="flex flex-col gap-1.5">
            <Select
              label="Years asked, at most"
              value={filters.max_years}
              options={[["", "Any"], ...YEARS.map((y) => [y, `${y} years`] as [string, string])]}
              onChange={(max_years) => onChange({ max_years })}
            />
            <label className={`flex items-center gap-2 text-xs ${anyYears ? "text-ink-3" : "text-ink-2"}`}>
              <input
                type="checkbox"
                className="size-3.5"
                checked={filters.include_unstated}
                disabled={anyYears}
                onChange={(e) => onChange({ include_unstated: e.target.checked })}
              />
              Include jobs that don&apos;t say
            </label>
          </div>
        </div>
        <div className="mt-4 flex justify-end gap-2 border-t border-line pt-3">
          {onClear && (
            <Button variant="quiet" size="sm" onClick={onClear}>
              Clear all
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Select({
  label,
  value,
  options,
  onChange,
  inline = false,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onChange: (value: string) => void;
  inline?: boolean;
}) {
  return (
    <label className={`flex text-xs font-semibold text-ink-3 ${inline ? "items-center gap-2" : "flex-col gap-1"}`}>
      {label}
      <select className={`${FIELD} pr-8 font-medium`} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Sends the typed text after a short pause, so each keystroke is not a server query. */
function SearchBox({ value, onChange }: { value: string; onChange: (q: string) => void }) {
  const [draft, setDraft] = useState(value);
  const [seen, setSeen] = useState(value);
  const [pending, setPending] = useState<string[]>([]); // sent, URL not yet showing them
  // An outside change (Clear filters) resets the box; a late echo of our own send must not.
  if (value !== seen) {
    setSeen(value);
    const i = pending.indexOf(value);
    if (i >= 0) {
      setPending(pending.slice(i + 1));
    } else {
      setDraft(value);
      setPending([]);
    }
  }

  useEffect(() => {
    if (draft === value || draft === pending.at(-1)) return;
    const t = setTimeout(() => {
      setPending((p) => [...p, draft]);
      onChange(draft);
    }, SEARCH_DELAY_MS);
    return () => clearTimeout(t);
  }, [draft, value, pending, onChange]);

  return (
    <label className="relative flex min-w-0 grow items-center">
      <span className="sr-only">Search title, company or text</span>
      <Icon name="search" className="pointer-events-none absolute left-3 text-ink-3" />
      <input
        type="search"
        className={`${FIELD} w-full pl-9`}
        value={draft}
        maxLength={MAX_SEARCH}
        placeholder="Search title, company or text"
        onChange={(e) => setDraft(e.target.value.slice(0, MAX_SEARCH))}
      />
    </label>
  );
}

/** Text box with suggestions; applies on pick, Enter or leaving the box. */
function Picker({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Facet[];
  onChange: (value: string) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    setDraft(value);
  }
  const commit = (text: string) => {
    const t = text.trim();
    if (t !== value) onChange(t);
  };

  return (
    <label className="flex flex-col gap-1 text-xs font-semibold text-ink-3">
      {label}
      <input
        className={`${FIELD} w-full font-medium`}
        list={id}
        value={draft}
        placeholder="Any"
        onChange={(e) => {
          const v = e.target.value;
          setDraft(v);
          if (v === "" || options.some((o) => o.name === v)) commit(v);
        }}
        onBlur={() => commit(draft)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit(draft);
        }}
      />
      <datalist id={id}>
        {options.map((o) => (
          <option key={o.name} value={o.name}>
            {`${o.name} (${o.count})`}
          </option>
        ))}
      </datalist>
    </label>
  );
}
