"use client";

import Link from "next/link";
import { istDate, useCachedApi, type Build } from "@/lib/api";
import { PdfLink } from "@/components/PdfLink";
import { ErrorNote, Refreshing, StateNote } from "@/components/ui";
import { Ornament } from "@/components/Ornament";

export default function ResumesPage() {
  const { data: builds, error, refreshing } = useCachedApi<Build[]>("/builds");

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-xl font-semibold">Resumes</h1>
        <Refreshing on={refreshing && builds !== null} />
      </div>
      <Ornament />
      <ErrorNote message={error} />
      {builds?.length === 0 && (
        <StateNote story="No resumes bound yet. Begin the tale in Coach.">No resumes built yet. Start from Coach.</StateNote>
      )}
      <ul className="page-turn flex flex-col gap-2">
        {builds?.map((b) => (
          <li key={b.id} className="card flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface-raised p-3">
            <Link href={`/coach/${b.session_id}`} className="font-medium hover:underline">
              {b.title || "Untitled role"} {b.company && `· ${b.company}`}
            </Link>
            <span className="text-xs text-ink-faint">{istDate(b.at)}</span>
            <span className="ml-auto flex gap-3 text-sm">
              <PdfLink buildId={b.id} />
              <a className="underline" href={`/api/builds/${b.id}/tex`}>.tex</a>
              <a className="underline" href={`/api/builds/${b.id}/report`} target="_blank" rel="noreferrer">
                Report
              </a>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
