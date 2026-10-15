"use client";

import { useCallback, useEffect, useId, useState } from "react";
import type { Facet, JobFacets } from "@/lib/api";
import { Button } from "@/components/ui";
import {
  DATE_LABELS,
  MAX_SEARCH,
  ROLE_LABELS,
  SORT_LABELS,
  STATUSES,
  WORK_MODE_LABELS,
  YEARS,
  type Filters,
} from "./filters";

const FIELD = "rounded border border-border-strong bg-surface-raised px-2 py-1.5 text-sm text-ink";
const SEARCH_DELAY_MS = 300;

type Props = {
  filters: Filters;
  facets: JobFacets | null;
  onChange: (patch: Partial<Filters>) => void;
  onClear: (() => void) | null;
};

export function FilterBar({ filters, facets, onChange, onClear }: Props) {
  const anyYears = filters.max_years === "";
  const setQ = useCallback((q: string) => onChange({ q }), [onChange]);
  return (
    <div className="card flex flex-wrap items-end gap-3 rounded-lg border border-border bg-surface-raised p-3">
      <SearchBox value={filters.q} onChange={setQ} />
      <Select
        label="Status"
        value={filters.status}
        options={STATUSES.map((s) => [s, s])}
        onChange={(status) => onChange({ status })}
      />
      <Select
        label="Years asked at most"
        value={filters.max_years}
        options={[["", "Any"], ...YEARS.map((y) => [y, y] as [string, string])]}
        onChange={(max_years) => onChange({ max_years })}
      />
      <label className={`flex items-center gap-1.5 py-1.5 text-sm ${anyYears ? "text-ink-faint" : "text-ink-muted"}`}>
        <input
          type="checkbox"
          checked={filters.include_unstated}
          disabled={anyYears}
          onChange={(e) => onChange({ include_unstated: e.target.checked })}
        />
        Include jobs that don&apos;t say
      </label>
      <Select
        label="Role"
        value={filters.role}
        options={[["", "Any"], ...Object.entries(ROLE_LABELS)]}
        onChange={(role) => onChange({ role })}
      />
      <Select
        label="Date posted"
        value={filters.posted_within_days}
        options={[["", "Any time"], ...Object.entries(DATE_LABELS)]}
        onChange={(posted_within_days) => onChange({ posted_within_days })}
      />
      <Select
        label="Work mode"
        value={filters.work_mode}
        options={[["", "Any"], ...Object.entries(WORK_MODE_LABELS)]}
        onChange={(work_mode) => onChange({ work_mode })}
      />
      <Picker label="City" value={filters.city} options={facets?.cities ?? []} onChange={(city) => onChange({ city })} />
      <Picker
        label="Company"
        value={filters.company}
        options={facets?.companies ?? []}
        onChange={(company) => onChange({ company })}
      />
      <Select
        label="Sort"
        value={filters.sort}
        options={Object.entries(SORT_LABELS)}
        onChange={(sort) => onChange({ sort })}
      />
      {onClear && (
        <Button variant="ghost" onClick={onClear}>
          Clear filters
        </Button>
      )}
    </div>
  );
}

export function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-0.5 text-xs text-ink-muted">
      {label}
      <select className={FIELD} value={value} onChange={(e) => onChange(e.target.value)}>
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
    <label className="flex min-w-48 grow flex-col gap-0.5 text-xs text-ink-muted">
      Search title, company or text
      <input
        type="search"
        className={FIELD}
        value={draft}
        maxLength={MAX_SEARCH}
        placeholder="e.g. python"
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
    <label className="flex flex-col gap-0.5 text-xs text-ink-muted">
      {label}
      <input
        className={`${FIELD} w-40`}
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
