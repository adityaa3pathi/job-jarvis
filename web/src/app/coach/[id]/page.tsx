"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { invalidate, istDate, post, runTask, TaskAborted, useCachedApi, type Session } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { PdfLink } from "@/components/PdfLink";
import { ReportView } from "@/components/ReportView";
import { Button, ErrorNote, FIELD, Panel, Refreshing, StateNote } from "@/components/ui";

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
  const log = useRef<HTMLDivElement>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  // Scroll the chat box only, so a new reply never jumps the whole page.
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
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

  if (!session) return read.error ? <ErrorNote message={read.error} /> : <StateNote>Loading session…</StateNote>;
  const pending = session.facts.filter((f) => !f.confirmed);
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <Link href="/coach" className="flex w-fit items-center gap-1 text-sm font-semibold text-ink-3 hover:text-ink">
          <Icon name="arrowLeft" size={14} />
          Coach
        </Link>
        <h1 className="text-2xl leading-tight font-extrabold tracking-tight text-balance">
          {session.title || "Untitled role"}
        </h1>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-ink-2">
          {session.company && <span>{session.company}</span>}
          {session.source_url && (
            <a className="flex items-center gap-1 font-semibold text-brand hover:underline" href={session.source_url} target="_blank" rel="noreferrer">
              Open job posting
              <Icon name="external" size={14} />
            </a>
          )}
          <Refreshing on={read.refreshing} />
        </div>
      </div>
      <ErrorNote message={read.error} />

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_400px]">
        <ReportView report={session.report} />

        <div className="flex flex-col gap-4 lg:sticky lg:top-[72px]">
          <Panel title="Talk it through">
            <div ref={log} className="-mx-4 -mt-4 flex max-h-[26rem] min-h-40 flex-col gap-2 overflow-y-auto px-4 pt-4 pb-1">
              {session.messages.length === 0 && (
                <p className="text-sm text-ink-3">
                  Ask anything: “Is my React work enough?”, “How do I explain the gap in Terraform?”. Tell the coach about
                  experience your summary misses; you confirm it before it goes on a resume.
                </p>
              )}
              {session.messages.map((m, i) => (
                <div
                  key={i}
                  className={`max-w-[88%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap ${
                    m.role === "user" ? "self-end rounded-br-md bg-brand text-brand-ink" : "self-start rounded-bl-md bg-sunk"
                  }`}
                >
                  {m.text}
                </div>
              ))}
            </div>
            <form onSubmit={send} className="mt-3 flex gap-2">
              <input
                aria-label="Message"
                className={`${FIELD} min-w-0 flex-1`}
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                placeholder="Ask the coach"
              />
              <Button disabled={!message.trim() || busy !== null} aria-label="Send">
                {busy === "chat" ? "Thinking…" : <Icon name="send" />}
              </Button>
            </form>
          </Panel>

          {session.facts.length > 0 && (
            <Panel title="New facts from the chat">
              <p className="mb-3 text-sm text-ink-2">Only confirmed facts are used on your resume.</p>
              <ul className="-mx-4 divide-y divide-line border-t border-line text-sm">
                {session.facts.map((f) => (
                  <li key={f.id} className="flex items-center gap-3 px-4 py-2.5">
                    <span className={`min-w-0 flex-1 ${f.confirmed ? "" : "text-ink-3"}`}>{f.text}</span>
                    <Button variant={f.confirmed ? "quiet" : "ghost"} size="sm" onClick={() => confirm(f.id, !f.confirmed)}>
                      {f.confirmed ? (
                        "Undo"
                      ) : (
                        <>
                          <Icon name="check" className="text-up" />
                          True
                        </>
                      )}
                    </Button>
                  </li>
                ))}
              </ul>
              <div className="mt-2">
                <ErrorNote message={factError} />
              </div>
            </Panel>
          )}

          <Panel title="Tailored resume">
            <div className="flex flex-col gap-3">
              <Button disabled={busy !== null} onClick={buildResume}>
                <Icon name="file" />
                {busy === "resume" ? "Building… (1–2 minutes)" : "Build my resume"}
              </Button>
              {pending.length > 0 && (
                <p className="text-sm text-warn">
                  {pending.length} {pending.length === 1 ? "fact is" : "facts are"} not confirmed yet and will be left out.
                </p>
              )}
              <ErrorNote message={error} />
              {lastReport && (
                <pre className="max-h-80 overflow-auto rounded-ctl bg-sunk p-3 text-xs whitespace-pre-wrap">{lastReport}</pre>
              )}
              {session.builds.length > 0 && (
                <ul className="-mx-4 divide-y divide-line border-t border-line text-sm">
                  {session.builds.map((b) => (
                    <li key={b.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5">
                      <span className="figure mr-auto text-ink-3">{istDate(b.at)}</span>
                      <PdfLink buildId={b.id} />
                      <a className="font-semibold text-brand hover:underline" href={`/api/builds/${b.id}/tex`}>
                        .tex
                      </a>
                      <a className="font-semibold text-brand hover:underline" href={`/api/builds/${b.id}/report`} target="_blank" rel="noreferrer">
                        What changed
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

export default function CoachSessionPage() {
  return (
    <Suspense fallback={<StateNote>Loading session…</StateNote>}>
      <CoachSession />
    </Suspense>
  );
}
