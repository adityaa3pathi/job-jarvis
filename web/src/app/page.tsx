"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  invalidate,
  istDateTime,
  mutate,
  post,
  useCachedApi,
  type DiscoverRun,
  type DiscoverState,
  type Job,
  type JobFacets,
  type JobPage,
} from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, Refreshing, StateNote } from "@/components/ui";
import { FilterBar, Select } from "@/components/jobs/FilterBar";
import { JobDetail, JobRow, coachHref } from "@/components/jobs/JobRow";
import { Pagination } from "@/components/jobs/Pagination";
import { SourceTabs } from "@/components/jobs/SourceTabs";
import {
  DEFAULTS,
  PAGE_SIZES,
  SORT_LABELS,
  STATUSES,
  apiQuery,
  isFiltered,
  readFilters,
  urlQuery,
  type Filters,
} from "@/components/jobs/filters";

const fmt = (n: number) => n.toLocaleString("en-IN");
const WIDE = "(min-width: 1024px)"; // the side pane shows; below it a tapped row opens in place
const STATUS_LABELS: Record<string, string> = {
  found: "To review",
  applied: "Applied",
  interview: "Interview",
  rejected: "Rejected",
  dropped: "Skipped",
  closed: "Closed",
};

export default function JobsPage() {
  // Reading the URL's filters needs a Suspense boundary under cacheComponents.
  return (
    <Suspense fallback={<StateNote>Loading jobs…</StateNote>}>
      <JobsView />
    </Suspense>
  );
}

function typing(target: EventTarget | null) {
  const el = target as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

function JobsView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters = useMemo(() => readFilters(params), [params]);
  const key = apiQuery(filters);
  const size = Number(filters.size);

  const path = `/jobs?${key}`;
  const facetsPath = `/jobs/facets?status=${filters.status}`;
  // The cache is keyed by path, so a slower reply for an older filter or page never replaces this one.
  const jobs = useCachedApi<JobPage>(path);
  const facetRead = useCachedApi<JobFacets>(facetsPath);
  const [discover, setDiscover] = useState<DiscoverState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number | null>(null); // null: the first row shows in the side pane
  const heading = useRef<HTMLHeadingElement>(null);
  const focusOnLoad = useRef(false);

  // The URL lags router.replace, so changes build on the newest request, not on `filters`.
  const latest = useRef(filters);
  const pending = useRef<string[]>([]); // URL queries requested but not yet shown
  const urlKey = urlQuery(filters);
  const synced = useRef(urlKey);
  useEffect(() => {
    if (urlKey === synced.current) return;
    synced.current = urlKey;
    const i = pending.current.indexOf(urlKey);
    pending.current = i >= 0 ? pending.current.slice(i + 1) : []; // not ours: back button, link
    if (pending.current.length === 0) latest.current = filters;
  }, [urlKey, filters]);

  const go = useCallback(
    (next: Filters) => {
      latest.current = next;
      const qs = urlQuery(next);
      if (qs !== synced.current || pending.current.length > 0) pending.current.push(qs);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );
  // Any filter or tab change starts again from page 1.
  const update = useCallback((patch: Partial<Filters>) => go({ ...latest.current, ...patch, page: 1 }), [go]);

  useEffect(() => {
    if (jobs.error) focusOnLoad.current = false; // else the next unrelated load would jump to the list
  }, [jobs.error]);

  const loaded = jobs.key === path ? jobs.data : null;
  const last = loaded ? Math.max(1, Math.ceil(loaded.total / size)) : null;
  const settled = loaded !== null && !jobs.refreshing; // a cached copy may hold an old total

  // A page past the end (after filtering or marking) moves to the last real page.
  useEffect(() => {
    // A newer request is on its way; its own reply decides.
    if (settled && last !== null && filters.page > last && pending.current.length === 0) go({ ...filters, page: last });
  }, [settled, last, filters, go]);

  useEffect(() => {
    if (!loaded || !focusOnLoad.current) return;
    focusOnLoad.current = false;
    heading.current?.scrollIntoView({ block: "start" });
    heading.current?.focus({ preventScroll: true });
  }, [loaded]);

  // A run started elsewhere (cron, another tab) or before a reload still shows.
  useEffect(() => {
    // A click's newer state may land first; this older reply must not overwrite it.
    api<DiscoverState>("/discover").then(
      (s) => setDiscover((prev) => prev ?? s),
      (e: Error) => setError(e.message),
    );
  }, []);

  useEffect(() => {
    if (!discover?.running) return;
    const t = setInterval(async () => {
      try {
        const s = await api<DiscoverState>("/discover");
        setDiscover(s);
        setError(null);
        if (!s.running) invalidate("/jobs");
      } catch (e) {
        setError((e as Error).message);
      }
    }, 4000);
    return () => clearInterval(t);
  }, [discover?.running]);

  // The last run's error is old news while a new run is going.
  const lastRun =
    discover && !discover.running && "id" in discover.last ? (discover.last as DiscoverRun) : null;

  const shown = jobs.data; // older results stay visible, dimmed, while new ones load
  const items = useMemo(() => shown?.items ?? [], [shown]);
  const selected = items.find((j) => j.id === picked) ?? items[0] ?? null;

  const mark = useCallback(
    async (job: Job, next: string) => {
      setError(null);
      const i = items.findIndex((j) => j.id === job.id);
      if (job.id === selected?.id) setPicked((items[i + 1] ?? items[i - 1])?.id ?? null); // keep a row in focus
      try {
        await post(`/jobs/${job.id}/mark`, { status: next });
        // Hide it now; refetching this page then fills the gap and fixes the counts.
        mutate<JobPage>(path, (d) => ({ ...d, items: d.items.filter((j) => j.id !== job.id) }));
        invalidate("/jobs");
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [items, selected, path],
  );

  // J/K move, A/S mark, C coach, O open: the watchlist is meant to be worked from the keyboard.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target) || !selected) return;
      const i = items.findIndex((j) => j.id === selected.id);
      const k = e.key.toLowerCase();
      if (k === "j" || k === "k") {
        const next = items[k === "j" ? Math.min(i + 1, items.length - 1) : Math.max(i - 1, 0)];
        setPicked(next.id);
        document.getElementById(`job-${next.id}`)?.scrollIntoView({ block: "nearest" });
      } else if (k === "a" && filters.status !== "applied") mark(selected, "applied");
      else if (k === "s" && filters.status !== "dropped") mark(selected, "dropped");
      else if (k === "c") router.push(coachHref(selected));
      else if (k === "o" && selected.url) window.open(selected.url, "_blank", "noreferrer");
      else return;
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selected, mark, router, filters.status]);

  function toPage(page: number) {
    focusOnLoad.current = true;
    go({ ...latest.current, page });
  }

  function step(delta: number) {
    toPage(Math.max(1, latest.current.page + delta));
  }

  const shownLast = shown ? Math.max(1, Math.ceil(shown.total / size)) : 1;
  const first = Number(new URLSearchParams(jobs.key?.split("?")[1]).get("offset") ?? 0); // of the page on screen
  const facets = facetRead.key === facetsPath ? facetRead.data : null;

  async function findNew() {
    setError(null);
    try {
      setDiscover(await post<DiscoverState>("/discover"));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <RunStrip discover={discover} lastRun={lastRun} onFind={findNew} />
      <ErrorNote message={error ?? jobs.error ?? facetRead.error ?? (lastRun?.error || null)} />

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(340px,420px)]">
        <section aria-busy={!loaded} className="flex min-w-0 flex-col gap-3">
          <div role="group" aria-label="List" className="-mx-4 flex gap-5 overflow-x-auto border-b border-line px-4 sm:mx-0 sm:px-0">
            {STATUSES.map((s) => {
              const on = filters.status === s;
              return (
                <button
                  key={s}
                  type="button"
                  aria-pressed={on}
                  onClick={() => update({ status: s })}
                  className={`relative shrink-0 pb-2.5 text-sm font-bold whitespace-nowrap ${
                    on ? "text-ink after:absolute after:inset-x-0 after:-bottom-px after:h-0.5 after:bg-brand" : "text-ink-3 hover:text-ink"
                  }`}
                >
                  {STATUS_LABELS[s] ?? s}
                </button>
              );
            })}
          </div>
          <FilterBar
            filters={filters}
            facets={facets}
            onChange={update}
            onClear={isFiltered({ ...filters, status: DEFAULTS.status }) ? () => go({ ...DEFAULTS, status: latest.current.status, size: latest.current.size }) : null}
          />
          <SourceTabs sources={shown?.sources ?? {}} active={filters.source} onSelect={(source) => update({ source })} />

          <div className="overflow-hidden rounded-lg border border-line bg-panel">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2.5">
              <h2 ref={heading} tabIndex={-1} className="scroll-mt-20 text-sm font-bold outline-none">
                {shown ? (
                  <>
                    <span className="figure">{fmt(shown.total)}</span> {shown.total === 1 ? "job" : "jobs"}
                    {shown.items.length > 0 && (
                      <span className="font-medium text-ink-3">
                        {" "}
                        · {fmt(first + 1)}–{fmt(first + shown.items.length)}
                      </span>
                    )}
                  </>
                ) : (
                  "Jobs"
                )}
              </h2>
              <Refreshing on={(jobs.refreshing && loaded !== null) || (shown !== null && !loaded)} className="mr-auto" />
              <Select
                inline
                label="Sort"
                value={filters.sort}
                options={Object.entries(SORT_LABELS)}
                onChange={(sort) => update({ sort })}
              />
            </div>
            {shown === null ? (
              <StateNote>Loading jobs…</StateNote>
            ) : items.length === 0 ? (
              <StateNote>
                {shown.total === 0 && loaded ? "No jobs match. Try fewer filters or another list." : "Loading jobs…"}
              </StateNote>
            ) : (
              <ul className={loaded ? "" : "opacity-60"}>
                {items.map((j) => (
                  <JobRow
                    key={j.id}
                    job={j}
                    status={filters.status}
                    selected={j.id === selected?.id}
                    expanded={j.id === picked}
                    onSelect={(job) => setPicked((p) => (p === job.id && !matchMedia(WIDE).matches ? null : job.id))}
                    onMark={mark}
                  />
                ))}
              </ul>
            )}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Pagination page={Math.min(filters.page, shownLast)} last={shownLast} onPage={toPage} onStep={step} />
            <Select
              inline
              label="Per page"
              value={filters.size}
              options={PAGE_SIZES.map((s) => [s, s])}
              onChange={(s) => update({ size: s })}
            />
          </div>
        </section>

        <aside
          aria-label="Selected job"
          className="sticky top-[72px] hidden max-h-[calc(100dvh-88px)] overflow-y-auto rounded-lg border border-line bg-panel px-5 pb-5 lg:block"
        >
          {selected ? (
            <JobDetail job={selected} status={filters.status} onMark={mark} />
          ) : (
            <StateNote>Pick a job to see it here.</StateNote>
          )}
        </aside>
      </div>
    </div>
  );
}

/** The top ticker: when the boards were last read and what moved, plus the button to read them now. */
function RunStrip({
  discover,
  lastRun,
  onFind,
}: {
  discover: DiscoverState | null;
  lastRun: DiscoverRun | null;
  onFind: () => void;
}) {
  const running = !!discover?.running;
  const cur = discover?.current;
  const run = running ? cur : lastRun;
  const sites = Object.keys(run?.portals ?? {}).length;
  const problems = [...(lastRun?.unreachable ?? []), ...(lastRun?.skipped ?? []), ...(lastRun?.notes ?? [])];
  const drops = Object.entries(lastRun?.drop_reasons ?? {});
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <h1 className="text-2xl font-extrabold tracking-tight">Jobs</h1>
        <dl className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
          <Stat label={running ? "Scanning since" : "Last scan"} value={run ? istDateTime(running ? run.started_at : (run.finished_at ?? run.started_at)) : "–"} />
          <Stat label="Read" value={run ? fmt(run.read ?? 0) : "–"} />
          <Stat label="New" value={run ? `+${fmt(run.new ?? 0)}` : "–"} tone={run?.new ? "text-up" : ""} />
          {sites > 0 && <Stat label="Sites" value={String(sites)} />}
          {lastRun?.status === "failed" && !running && <Stat label="Status" value="Failed" tone="text-down" />}
        </dl>
        <Button className="ml-auto" disabled={running} onClick={onFind}>
          <Icon name="refresh" className={running ? "motion-safe:animate-spin" : ""} />
          {running ? "Scanning boards…" : "Find new jobs"}
        </Button>
      </div>
      {!running && lastRun && (Object.keys(lastRun.portals ?? {}).length > 0 || problems.length > 0) && (
        <details className="group text-sm">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 font-semibold text-ink-3 hover:text-ink">
            <Icon name="chevron" size={14} className="transition-transform group-open:rotate-90" />
            Site report
            {problems.length > 0 && <span className="text-warn"> · {problems.length} notes</span>}
          </summary>
          <div className="mt-3 overflow-x-auto rounded-lg border border-line bg-panel">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-ink-3">
                <tr className="border-b border-line">
                  <th className="px-4 py-2 font-semibold">Site</th>
                  <th className="px-4 py-2 text-right font-semibold">Read</th>
                  <th className="px-4 py-2 text-right font-semibold">New</th>
                  <th className="px-4 py-2 text-right font-semibold">Saved</th>
                </tr>
              </thead>
              <tbody className="figure">
                {Object.entries(lastRun.portals ?? {})
                  .sort((a, b) => b[1].new - a[1].new)
                  .map(([name, c]) => (
                    <tr key={name} className="border-b border-line last:border-b-0">
                      <td className="px-4 py-1.5 font-semibold">{name}</td>
                      <td className="px-4 py-1.5 text-right">{fmt(c.read)}</td>
                      <td className={`px-4 py-1.5 text-right ${c.new ? "text-up" : "text-ink-3"}`}>{c.new ? `+${fmt(c.new)}` : "0"}</td>
                      <td className="px-4 py-1.5 text-right">{fmt(c.saved)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
            {(drops.length > 0 || problems.length > 0) && (
              <div className="border-t border-line px-4 py-3 text-xs">
                {drops.length > 0 && (
                  <p className="text-ink-2">
                    Left out: {drops.map(([k, v]) => `${k.replaceAll("_", " ")} ${fmt(v)}`).join(" · ")}
                  </p>
                )}
                {problems.length > 0 && (
                  <ul className="mt-2 flex flex-col gap-1 text-warn">
                    {problems.map((line, i) => (
                      <li key={i}>{line}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

function Stat({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-xs font-semibold text-ink-3">{label}</dt>
      <dd className={`figure font-bold ${tone}`}>{value}</dd>
    </div>
  );
}
