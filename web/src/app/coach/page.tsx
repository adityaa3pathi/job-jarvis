"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { ApiError, invalidate, istDate, runTask, TaskAborted, useCachedApi, type SessionRow } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, FIELD, PageHead, Panel, Refreshing } from "@/components/ui";

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
      invalidate("/coach");
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
    <Panel
      title="Check a job against your profile"
      aside={
        <div role="tablist" aria-label="Job source" className="flex rounded-ctl bg-sunk p-0.5">
          {(["link", "text"] as const).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`h-7 rounded px-3 text-xs font-bold ${tab === t ? "bg-panel text-ink shadow-sm" : "text-ink-3 hover:text-ink"}`}
            >
              {t === "link" ? "Link" : "Text"}
            </button>
          ))}
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        {tab === "link" ? (
          <input
            aria-label="Job link"
            placeholder="https://… paste the job posting link"
            className={`${FIELD} h-11 w-full text-[15px]`}
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
            className={`${FIELD} h-56 w-full py-2.5 leading-relaxed`}
            value={jdText}
            onChange={(e) => setJdText(e.target.value)}
          />
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <input
            aria-label="Job title (optional)"
            placeholder="Job title (optional)"
            className={FIELD}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <input
            aria-label="Company (optional)"
            placeholder="Company (optional)"
            className={FIELD}
            value={company}
            onChange={(e) => setCompany(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button disabled={!ready || busy} onClick={() => start()}>
            {busy ? "Reading the job… (up to a minute)" : "Check my fit"}
          </Button>
          {askTrust && (
            <Button variant="ghost" disabled={busy} onClick={() => start(true)}>
              Trust this site and read it
            </Button>
          )}
        </div>
        <ErrorNote message={error} />
      </div>
    </Panel>
  );
}

function PastSessions() {
  const { data: rows, refreshing } = useCachedApi<SessionRow[]>("/coach");
  if (!rows?.length) return null;
  return (
    <Panel title="Past sessions" aside={<Refreshing on={refreshing} />}>
      <ul className="-mx-4 -my-4 divide-y divide-line">
        {rows.map((r) => (
          <li key={r.id}>
            <Link href={`/coach/${r.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-sunk">
              <span className="min-w-0 flex-1">
                <span className="block truncate font-bold">{r.title || "Untitled role"}</span>
                {r.company && <span className="block truncate text-sm text-ink-2">{r.company}</span>}
              </span>
              <span className="figure text-xs text-ink-3">{istDate(r.created_at)}</span>
              <Icon name="chevron" className="text-ink-3" />
            </Link>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export default function CoachPage() {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5">
      <PageHead title="Coach" />
      <Suspense>
        <NewCoach />
      </Suspense>
      <PastSessions />
    </div>
  );
}
