"use client";

import { useParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { invalidate, istDate, post, runTask, TaskAborted, useCachedApi, type Session } from "@/lib/api";
import { PdfLink } from "@/components/PdfLink";
import { ReportView } from "@/components/ReportView";
import { Button, Card, ErrorNote, Refreshing, StateNote } from "@/components/ui";

function CoachSession() {
  const { id } = useParams<{ id: string }>();
  const path = `/coach/${id}`;
  const read = useCachedApi<Session>(path);
  const session = read.key === path ? read.data : null; // never show another session's page
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState<"chat" | "resume" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastReport, setLastReport] = useState<string | null>(null);
  const [factError, setFactError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [session?.messages.length]);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    setBusy("chat");
    setError(null);
    try {
      await runTask(`/coach/${id}/chat`, { message }, alive.current?.signal);
      setMessage("");
      await invalidate(path);
    } catch (err) {
      if (err instanceof TaskAborted) return;
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function confirm(factId: number, confirmed: boolean) {
    setFactError(null);
    try {
      await post(`/coach/${id}/facts/${factId}`, { confirmed });
      await invalidate(path);
    } catch (err) {
      setFactError((err as Error).message);
    }
  }

  async function buildResume() {
    setBusy("resume");
    setError(null);
    try {
      const out = await runTask<{ build_id: number; report: string }>(
        `/coach/${id}/resume`,
        undefined,
        alive.current?.signal,
      );
      setLastReport(out.report);
      invalidate("/builds");
      await invalidate(path);
    } catch (err) {
      if (err instanceof TaskAborted) return;
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (!session) return read.error ? <ErrorNote message={read.error} /> : <StateNote story="Turning to the right page…">Loading…</StateNote>;
  const pending = session.facts.filter((f) => !f.confirmed);
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">
          {session.title || "Untitled role"} {session.company && <span className="text-ink-faint">· {session.company}</span>}
        </h1>
        <Refreshing on={read.refreshing} />
        {session.source_url && (
          <a className="text-sm text-ink-faint underline" href={session.source_url} target="_blank" rel="noreferrer">
            Open job posting
          </a>
        )}
      </div>
      <ErrorNote message={read.error} />
      <ReportView report={session.report} />

      <Card title="Talk it through">
        <div className="flex max-h-96 flex-col gap-2 overflow-y-auto">
          {session.messages.length === 0 && (
            <p className="text-sm text-ink-faint">
              Ask anything: “Is my React work enough?”, “How do I explain the gap in Terraform?”. Tell the coach about
              experience your summary misses; you confirm it before it goes on a resume.
            </p>
          )}
          {session.messages.map((m, i) => (
            <div
              key={i}
              className={`max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
                m.role === "user" ? "self-end bg-accent text-accent-ink" : "self-start bg-surface-muted"
              }`}
            >
              {m.text}
            </div>
          ))}
          <div ref={bottom} />
        </div>
        <form onSubmit={send} className="mt-3 flex gap-2">
          <input
            aria-label="Message"
            className="flex-1 rounded border border-border-strong px-3 py-2 text-sm"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Type a message"
          />
          <Button disabled={!message.trim() || busy !== null}>{busy === "chat" ? "Thinking…" : "Send"}</Button>
        </form>
      </Card>

      {session.facts.length > 0 && (
        <Card title="New facts from the chat">
          <p className="mb-2 text-sm text-ink-muted">Only confirmed facts are used on your resume.</p>
          <ul className="flex flex-col gap-2 text-sm">
            {session.facts.map((f) => (
              <li key={f.id} className="flex items-center gap-2">
                <span className={f.confirmed ? "" : "text-ink-faint"}>{f.text}</span>
                <Button variant="ghost" className="ml-auto" onClick={() => confirm(f.id, !f.confirmed)}>
                  {f.confirmed ? "Undo" : "This is true"}
                </Button>
              </li>
            ))}
          </ul>
          <div className="mt-2">
            <ErrorNote message={factError} />
          </div>
        </Card>
      )}

      <Card title="Tailored resume">
        <div className="flex flex-wrap items-center gap-3">
          <Button disabled={busy !== null} onClick={buildResume}>
            {busy === "resume" ? "Building… (1–2 minutes)" : "Build my resume"}
          </Button>
          {pending.length > 0 && (
            <span className="text-sm text-warning">{pending.length} fact(s) not confirmed yet; they will be left out.</span>
          )}
        </div>
        <div className="mt-2">
          <ErrorNote message={error} />
        </div>
        {lastReport && (
          <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-surface p-3 text-xs">{lastReport}</pre>
        )}
        {session.builds.length > 0 && (
          <ul className="mt-3 flex flex-col gap-1 text-sm">
            {session.builds.map((b) => (
              <li key={b.id} className="flex gap-3">
                <span className="text-ink-faint">{istDate(b.at)}</span>
                <PdfLink buildId={b.id} />
                <a className="underline" href={`/api/builds/${b.id}/tex`}>.tex</a>
                <a className="underline" href={`/api/builds/${b.id}/report`} target="_blank" rel="noreferrer">
                  What changed
                </a>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export default function CoachSessionPage() {
  return (
    <Suspense fallback={<StateNote story="Turning to the right page…">Loading…</StateNote>}>
      <CoachSession />
    </Suspense>
  );
}
