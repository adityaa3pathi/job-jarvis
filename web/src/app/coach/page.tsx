"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { api, ApiError, istDate, runTask, TaskAborted, type SessionRow } from "@/lib/api";
import { Button, Card, ErrorNote } from "@/components/ui";
import { Ornament } from "@/components/Ornament";

function NewCoach() {
  const params = useSearchParams();
  const router = useRouter();
  const [tab, setTab] = useState<"link" | "text">(params.get("url") ? "link" : "text");
  const [url, setUrl] = useState(params.get("url") ?? "");
  const [jdText, setJdText] = useState("");
  const [title, setTitle] = useState(params.get("title") ?? "");
  const [company, setCompany] = useState(params.get("company") ?? "");
  const [askTrust, setAskTrust] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  async function start(trustHost = false) {
    setBusy(true);
    setError(null);
    try {
      const body =
        tab === "link"
          ? { url, title, company, trust_host: trustHost }
          : { jd_text: jdText, title, company };
      const signal = alive.current?.signal;
      const { session_id } = await runTask<{ session_id: number }>("/coach", body, signal);
      if (!signal?.aborted) router.push(`/coach/${session_id}`);
    } catch (e) {
      if (e instanceof TaskAborted) return;
      if (e instanceof ApiError && e.status === 409) setAskTrust(true);
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const ready = tab === "link" ? url.startsWith("https://") : jdText.trim().length > 50;
  return (
    <Card title="Coach me on a job">
      <div role="tablist" className="mb-3 flex gap-2">
        {(["link", "text"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`rounded px-3 py-1 text-sm ${tab === t ? "bg-accent text-accent-ink" : "bg-surface-muted"}`}
          >
            {t === "link" ? "Paste link" : "Paste JD text"}
          </button>
        ))}
      </div>
      {tab === "link" ? (
        <input
          aria-label="Job link"
          placeholder="https://…"
          className="w-full rounded border border-border-strong px-3 py-2"
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setAskTrust(false);
          }}
        />
      ) : (
        <textarea
          aria-label="Job description"
          placeholder="Paste the full job description here"
          className="h-56 w-full rounded border border-border-strong p-2 text-sm"
          value={jdText}
          onChange={(e) => setJdText(e.target.value)}
        />
      )}
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <input
          aria-label="Job title (optional)"
          placeholder="Job title (optional)"
          className="rounded border border-border-strong px-3 py-2 text-sm"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <input
          aria-label="Company (optional)"
          placeholder="Company (optional)"
          className="rounded border border-border-strong px-3 py-2 text-sm"
          value={company}
          onChange={(e) => setCompany(e.target.value)}
        />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button disabled={!ready || busy} onClick={() => start()}>
          {busy ? "Reading the job… (up to a minute)" : "Analyse"}
        </Button>
        {askTrust && (
          <Button variant="ghost" disabled={busy} onClick={() => start(true)}>
            Trust this site and read it
          </Button>
        )}
      </div>
      <div className="mt-2">
        <ErrorNote message={error} />
      </div>
    </Card>
  );
}

function PastSessions() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  useEffect(() => {
    api<SessionRow[]>("/coach").then(setRows, () => setRows([]));
  }, []);
  if (!rows?.length) return null;
  return (
    <Card title="Past sessions">
      <ul className="flex flex-col gap-1 text-sm">
        {rows.map((r) => (
          <li key={r.id} className="flex gap-2">
            <Link href={`/coach/${r.id}`} className="hover:underline">
              {r.title || "Untitled role"} {r.company && `· ${r.company}`}
            </Link>
            <span className="ml-auto text-ink-faint">{istDate(r.created_at)}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export default function CoachPage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="font-display text-xl font-semibold">Coach</h1>
      <Ornament />
      <Suspense>
        <NewCoach />
      </Suspense>
      <PastSessions />
    </div>
  );
}
