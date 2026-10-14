"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, istDate, post, type Job } from "@/lib/api";
import { Button, ErrorNote } from "@/components/ui";

type DiscoverState = {
  running: boolean;
  last: { read?: number; new?: number; unreachable?: string[]; error?: string };
};

const STATUSES = ["found", "applied", "interview", "rejected", "dropped"];

export default function JobsPage() {
  const [status, setStatus] = useState("found");
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [discover, setDiscover] = useState<DiscoverState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => api<Job[]>(`/jobs?status=${status}`).then(setJobs, (e: Error) => setError(e.message)),
    [status],
  );

  useEffect(() => {
    api<Job[]>(`/jobs?status=${status}`).then(setJobs, (e: Error) => setError(e.message));
  }, [status]);

  useEffect(() => {
    if (!discover?.running) return;
    const t = setInterval(async () => {
      try {
        const s = await api<DiscoverState>("/discover");
        setDiscover(s);
        if (!s.running) load();
      } catch (e) {
        setError((e as Error).message);
      }
    }, 4000);
    return () => clearInterval(t);
  }, [discover?.running, load]);

  async function mark(id: number, next: string) {
    setError(null);
    try {
      await post(`/jobs/${id}/mark`, { status: next });
      setJobs((js) => js?.filter((j) => j.id !== id) ?? null);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-auto text-xl font-semibold">Jobs</h1>
        <select
          aria-label="Status"
          className="rounded border border-zinc-300 px-2 py-1.5 text-sm"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          {STATUSES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
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
      {discover && !discover.running && discover.last.read !== undefined && (
        <p className="text-sm text-zinc-600">
          Read {discover.last.read} jobs, {discover.last.new} new.
          {discover.last.unreachable?.length ? ` Could not reach: ${discover.last.unreachable.join(", ")}` : ""}
        </p>
      )}
      <ErrorNote message={error ?? discover?.last?.error ?? null} />
      {jobs === null ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : jobs.length === 0 ? (
        <p className="text-sm text-zinc-500">No jobs with status “{status}”.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {jobs.map((j) => (
            <li key={j.id} className="rounded-lg border border-zinc-200 bg-white p-3">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <a href={j.url ?? "#"} target="_blank" rel="noreferrer" className="font-medium hover:underline">
                  {j.title}
                </a>
                <span className="text-zinc-600">· {j.company}</span>
                <span className="ml-auto text-xs text-zinc-500">
                  {j.source} · {istDate(j.posted_date ?? j.seen_date)}
                </span>
              </div>
              <p className="text-sm text-zinc-600">
                {[j.location, j.experience_text, j.drop_reason].filter(Boolean).join(" · ")}
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <Link
                  className="rounded bg-zinc-900 px-3 py-1 text-sm text-white hover:bg-zinc-700"
                  href={`/coach?url=${encodeURIComponent(j.url ?? "")}&title=${encodeURIComponent(j.title)}&company=${encodeURIComponent(j.company)}`}
                >
                  Coach me
                </Link>
                {status !== "applied" && (
                  <Button variant="ghost" onClick={() => mark(j.id, "applied")}>
                    Applied
                  </Button>
                )}
                {status !== "dropped" && (
                  <Button variant="ghost" onClick={() => mark(j.id, "dropped")}>
                    Skip
                  </Button>
                )}
                {status !== "found" && (
                  <Button variant="ghost" onClick={() => mark(j.id, "found")}>
                    Back to list
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
