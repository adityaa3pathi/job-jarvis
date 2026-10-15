"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  istDateTime,
  post,
  type DiscoverRun,
  type DiscoverState,
  type Job,
  type JobFacets,
  type JobPage,
} from "@/lib/api";
import { Button, ErrorNote } from "@/components/ui";
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
    <Suspense fallback={<p className="text-sm text-zinc-500">Loading…</p>}>
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

  const [data, setData] = useState<(JobPage & { key: string }) | null>(null);
  const [reloads, setReloads] = useState(0);
  const [facets, setFacets] = useState<(JobFacets & { status: string }) | null>(null);
  const [discover, setDiscover] = useState<DiscoverState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const focusOnLoad = useRef(false);

  const go = useCallback(
    (next: Filters) => {
      const qs = urlQuery(next);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );
  // Any filter or tab change starts again from page 1.
  const update = useCallback((patch: Partial<Filters>) => go({ ...filters, ...patch, page: 1 }), [go, filters]);

  useEffect(() => {
    let current = true; // a slower reply for an older filter or page must not replace this one
    api<JobPage>(`/jobs?${key}`).then(
      (p) => current && setData({ ...p, key }),
      (e: Error) => current && setError(e.message),
    );
    return () => {
      current = false;
    };
  }, [key, reloads]);

  useEffect(() => {
    let current = true;
    api<JobFacets>(`/jobs/facets?status=${filters.status}`).then(
      (f) => current && setFacets({ ...f, status: filters.status }),
      (e: Error) => current && setError(e.message),
    );
    return () => {
      current = false;
    };
  }, [filters.status]);

  const loaded = data?.key === key ? data : null;
  const last = loaded ? Math.max(1, Math.ceil(loaded.total / size)) : null;

  // A page past the end (after filtering or marking) moves to the last real page.
  useEffect(() => {
    if (last !== null && filters.page > last) go({ ...filters, page: last });
  }, [last, filters, go]);

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
        if (!s.running) setReloads((n) => n + 1);
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
      setData((d) => d && { ...d, items: d.items.filter((j) => j.id !== job.id) });
      setReloads((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function toPage(page: number) {
    focusOnLoad.current = true;
    go({ ...filters, page });
  }

  const shown = loaded ?? data; // older results stay visible, dimmed, while new ones load
  const shownLast = shown ? Math.max(1, Math.ceil(shown.total / size)) : 1;
  const first = Number(new URLSearchParams(shown?.key).get("offset") ?? 0); // of the page on screen

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-auto text-xl font-semibold">Jobs</h1>
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
      {discover?.running ? (
        <p className="text-sm text-zinc-600">
          Searching boards
          {discover.current
            ? ` since ${istDateTime(discover.current.started_at)}: read ${discover.current.read ?? 0}, ${discover.current.new ?? 0} new so far.`
            : "…"}
        </p>
      ) : (
        lastRun && <RunSummary run={lastRun} />
      )}
      <ErrorNote message={error ?? (lastRun?.error || null)} />
      <SourceTabs sources={shown?.sources ?? {}} active={filters.source} onSelect={(source) => update({ source })} />
      <FilterBar
        filters={filters}
        facets={facets?.status === filters.status ? facets : null}
        onChange={update}
        onClear={isFiltered(filters) ? () => go({ ...DEFAULTS, size: filters.size }) : null}
      />
      {shown === null ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : (
        <section aria-busy={!loaded} className="flex flex-col gap-2">
          <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
            <div className="mr-auto">
              <h2 ref={heading} tabIndex={-1} className="scroll-mt-4 font-medium outline-none">
                {fmt(shown.total)} {shown.total === 1 ? "job matches" : "jobs match"}
              </h2>
              {shown.items.length > 0 && (
                <p className="text-sm text-zinc-600">
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
            <p className="text-sm text-zinc-500">
              {shown.total === 0 && loaded ? "No jobs match these filters." : "Loading…"}
            </p>
          ) : (
            <ul className={`flex flex-col gap-2 ${loaded ? "" : "opacity-60"}`}>
              {shown.items.map((j) => (
                <JobCard key={j.id} job={j} status={filters.status} onMark={mark} />
              ))}
            </ul>
          )}
          <Pagination
            page={Math.min(filters.page, shownLast)}
            last={shownLast}
            onPage={toPage}
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
    <div className="text-sm text-zinc-600">
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
              <li key={`n-${i}`} className="text-amber-700">
                {line}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
