"use client";

import Link from "next/link";
import { istDate, useCachedApi, type Build } from "@/lib/api";
import { PdfLink } from "@/components/PdfLink";
import { ErrorNote, PageHead, Refreshing, StateNote, buttonClass } from "@/components/ui";

export default function ResumesPage() {
  const { data: builds, error, refreshing } = useCachedApi<Build[]>("/builds");

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <PageHead title="Resumes">
        <Refreshing on={refreshing && builds !== null} />
      </PageHead>
      <ErrorNote message={error} />
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        {builds === null ? (
          <StateNote>Loading resumes…</StateNote>
        ) : builds.length === 0 ? (
          <div className="flex flex-col items-center gap-3 px-4 py-12 text-center">
            <p className="font-bold">No resumes yet</p>
            <p className="max-w-sm text-sm text-ink-2">
              Open a job in Coach, check your fit, then build a resume tuned to it from facts you confirmed.
            </p>
            <Link href="/coach" className={buttonClass("primary")}>
              Go to Coach
            </Link>
          </div>
        ) : (
          <ul className="divide-y divide-line">
            {builds.map((b) => (
              <li key={b.id} className="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 py-3 hover:bg-sunk/60">
                <Link href={`/coach/${b.session_id}`} className="min-w-0 flex-1 basis-60 hover:underline">
                  <span className="block truncate font-bold">{b.title || "Untitled role"}</span>
                  {b.company && <span className="block truncate text-sm text-ink-2">{b.company}</span>}
                </Link>
                <span className="figure text-xs text-ink-3">{istDate(b.at)}</span>
                <span className="flex gap-4 text-sm">
                  <PdfLink buildId={b.id} />
                  <a className="font-semibold text-brand hover:underline" href={`/api/builds/${b.id}/tex`}>
                    .tex
                  </a>
                  <a className="font-semibold text-brand hover:underline" href={`/api/builds/${b.id}/report`} target="_blank" rel="noreferrer">
                    Report
                  </a>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
