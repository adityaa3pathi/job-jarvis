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
import { Button, ErrorNote, Refreshing, StateNote } from "@/components/ui";
import { Ornament } from "@/components/Ornament";
import { FilterBar, Select } from "@/components/jobs/FilterBar";
import { JobCard } from "@/components/jobs/JobCard";
import { Pagination } from "@/components/jobs/Pagination";
import { SourceTabs } from "@/components/jobs/SourceTabs";
import {
  DEFAULTS,
  PAGE_SIZES,
  apiQuery,
  isFiltered,
  readFilters,
  urlQuery,
  type Filters,
} from "@/components/jobs/filters";

const fmt = (n: number) => n.toLocaleString("en-IN");

export default function JobsPage() {
  // Reading the URL's filters needs a Suspense boundary under cacheComponents.
  return (
    <Suspense fallback={<StateNote story="Unrolling the scrolls…">Loading…</StateNote>}>
      <JobsView />
    </Suspense>
  );
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

  async function mark(job: Job, next: string) {
    setError(null);
    try {
      await post(`/jobs/${job.id}/mark`, { status: next });
      // Hide it now; refetching this page then fills the gap and fixes the counts.
      mutate<JobPage>(path, (d) => ({ ...d, items: d.items.filter((j) => j.id !== job.id) }));
      invalidate("/jobs");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function toPage(page: number) {
    focusOnLoad.current = true;
    go({ ...latest.current, page });
  }

  function step(delta: number) {
    toPage(Math.max(1, latest.current.page + delta));
  }

  const shown = jobs.data; // older results stay visible, dimmed, while new ones load
  const shownLast = shown ? Math.max(1, Math.ceil(shown.total / size)) : 1;
  const first = Number(new URLSearchParams(jobs.key?.split("?")[1]).get("offset") ?? 0); // of the page on screen
  const facets = facetRead.key === facetsPath ? facetRead.data : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="font-display text-xl font-semibold">Jobs</h1>
        <Refreshing on={jobs.refreshing && loaded !== null} className="mr-auto" />
        <Button
          disabled={discover?.running}
          onClick={async () => {
            setError(null);
            try {
              setDiscover(await post<DiscoverState>("/discover"));
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          {discover?.running ? "Searching boards…" : "Find new jobs"}
        </Button>
      </div>
      <Ornament />
      {discover?.running ? (
        <p className="text-sm text-ink-muted">
          Searching boards
          {discover.current
            ? ` since ${istDateTime(discover.current.started_at)}: read ${discover.current.read ?? 0}, ${discover.current.new ?? 0} new so far.`
            : "…"}
        </p>
      ) : (
        lastRun && <RunSummary run={lastRun} />
      )}
      <ErrorNote message={error ?? jobs.error ?? facetRead.error ?? (lastRun?.error || null)} />
      <SourceTabs sources={shown?.sources ?? {}} active={filters.source} onSelect={(source) => update({ source })} />
      <FilterBar
        filters={filters}
        facets={facets}
        onChange={update}
        onClear={isFiltered(filters) ? () => go({ ...DEFAULTS, size: latest.current.size }) : null}
      />
      {shown === null ? (
        <StateNote story="Unrolling the scrolls…">Loading…</StateNote>
      ) : (
        <section aria-busy={!loaded} className="flex flex-col gap-2">
          <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
            <div className="mr-auto">
              <h2 ref={heading} tabIndex={-1} className="scroll-mt-4 font-medium outline-none">
                {fmt(shown.total)} {shown.total === 1 ? "job matches" : "jobs match"}
              </h2>
              {shown.items.length > 0 && (
                <p className="text-sm text-ink-muted">
                  Showing {fmt(first + 1)}–{fmt(first + shown.items.length)} of {fmt(shown.total)}
                  {!loaded && " · updating…"}
                </p>
              )}
            </div>
            <Select
              label="Per page"
              value={filters.size}
              options={PAGE_SIZES.map((s) => [s, s])}
              onChange={(s) => update({ size: s })}
            />
          </div>
          {shown.items.length === 0 ? (
            shown.total === 0 && loaded ? (
              <StateNote story="The scrolls are empty: no jobs match these filters.">No jobs match these filters.</StateNote>
            ) : (
              <StateNote story="Unrolling the scrolls…">Loading…</StateNote>
            )
          ) : (
            <ul className={`page-turn flex flex-col gap-2 ${loaded ? "" : "opacity-60"}`}>
              {shown.items.map((j) => (
                <JobCard key={j.id} job={j} status={filters.status} onMark={mark} />
              ))}
            </ul>
          )}
          <Pagination
            page={Math.min(filters.page, shownLast)}
            last={shownLast}
            onPage={toPage}
            onStep={step}
          />
        </section>
      )}
    </div>
  );
}

function RunSummary({ run }: { run: DiscoverRun }) {
  const portals = Object.entries(run.portals ?? {});
  const drops = Object.entries(run.drop_reasons ?? {});
  const problems = [...(run.unreachable ?? []), ...(run.skipped ?? []), ...(run.notes ?? [])];
  return (
    <div className="sparkle text-sm text-ink-muted">
      <p>
        Last search {run.status === "failed" ? "failed" : "finished"} {istDateTime(run.finished_at ?? run.started_at)}
        : read {run.read ?? 0} jobs, {run.new ?? 0} new
        {drops.length ? `, dropped ${drops.map(([k, v]) => `${k} ${v}`).join(", ")}` : ""}.
        {run.unreachable?.length ? ` Could not reach ${run.unreachable.length} site(s).` : ""}
      </p>
      {(portals.length > 0 || problems.length > 0) && (
        <details className="mt-1">
          <summary className="cursor-pointer">Details per site</summary>
          <ul className="mt-1 list-disc pl-5">
            {portals.map(([name, c]) => (
              <li key={`p-${name}`}>
                {name}: read {c.read}, new {c.new}, saved {c.saved}
              </li>
            ))}
            {problems.map((line, i) => (
              <li key={`n-${i}`} className="text-warning">
                {line}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
