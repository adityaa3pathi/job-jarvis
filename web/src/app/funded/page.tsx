"use client";

import Link from "next/link";
import { istDate, useCachedApi, type FundingRow } from "@/lib/api";
import { ErrorNote, PageHead, Refreshing, StateNote, Tag } from "@/components/ui";

/** Where a funded company stands for the owner: open jobs, watched with none, or unknown. */
function Standing({ row }: { row: FundingRow }) {
  if (row.open_jobs > 0)
    return (
      <Link href={`/?company=${encodeURIComponent(row.job_company)}`} className="text-sm font-semibold text-brand hover:underline">
        {row.open_jobs} open {row.open_jobs === 1 ? "job" : "jobs"}
      </Link>
    );
  if (row.watched) return <span className="text-sm text-ink-3">Board watched, no open jobs</span>;
  return <Tag tone="warn">Not watched yet</Tag>;
}

export default function FundedPage() {
  const { data: rows, error, refreshing } = useCachedApi<FundingRow[]>("/funding");

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <PageHead title="Recently funded">
        <Refreshing on={refreshing && rows !== null} />
      </PageHead>
      <p className="text-sm text-ink-2">
        Startups that raised money in the last 4 months, from Inc42 and Entrackr news. Freshly funded teams hire. &quot;Not
        watched yet&quot; means their job board is not on your company list.
      </p>
      <ErrorNote message={error} />
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        {rows === null ? (
          <StateNote>Loading funding news…</StateNote>
        ) : rows.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-12 text-center">
            <p className="font-bold">No funding news yet</p>
            <p className="max-w-sm text-sm text-ink-2">It is read with every job scan. Run Find new jobs, then check back.</p>
          </div>
        ) : (
          <ul className="divide-y divide-line">
            {rows.map((r) => (
              <li key={r.url} className="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 py-3 hover:bg-sunk/60">
                <span className="min-w-0 flex-1 basis-72">
                  <span className="block truncate font-bold">{r.company}</span>
                  <a href={r.url} target="_blank" rel="noreferrer" className="block text-sm text-ink-2 hover:underline">
                    {r.headline}
                  </a>
                </span>
                <span className="figure text-xs text-ink-3">{istDate(r.published)}</span>
                <Standing row={r} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
