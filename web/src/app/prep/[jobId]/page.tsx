"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { invalidate, istDate, runTask, TaskAborted, useCachedApi, type PrepQuestion, type PrepView } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, Panel, Refreshing, StateNote, Tag } from "@/components/ui";

const KINDS: { kind: PrepQuestion["kind"]; title: string }[] = [
  { kind: "about_you", title: "About your work" },
  { kind: "behavioral", title: "Behavioral" },
  { kind: "technical", title: "Technical" },
  { kind: "company", title: "About the company and role" },
];

function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="flex list-disc flex-col gap-1 pl-5 text-sm">
      {items.map((t, i) => (
        <li key={i}>{t}</li>
      ))}
    </ul>
  );
}

function Question({ q }: { q: PrepQuestion }) {
  return (
    <li className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
      <p className="flex flex-wrap items-center gap-2 font-semibold">
        {q.question}
        {!q.grounded && <Tag tone="warn">Check this: not found in your profile</Tag>}
      </p>
      <Bullets items={q.answer} />
      {q.based_on && <p className="text-xs text-ink-3">From your profile: &ldquo;{q.based_on}&rdquo;</p>}
    </li>
  );
}

function JobPrep() {
  const { jobId } = useParams<{ jobId: string }>();
  const path = `/prep/${jobId}`;
  const read = useCachedApi<PrepView>(path);
  const data = read.key === path ? read.data : null;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  async function make() {
    setError(null);
    setBusy(true);
    try {
      await runTask(path, undefined, alive.current?.signal);
      await invalidate(path);
    } catch (e) {
      if (e instanceof TaskAborted) return;
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const prep = data?.prep ?? null;
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5">
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/tracker" className="text-sm font-semibold text-ink-2 hover:text-ink">
          ← Tracker
        </Link>
        <Refreshing on={read.refreshing && data !== null} />
      </div>
      {data && (
        <header className="flex flex-wrap items-end gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl font-extrabold tracking-tight">Interview prep</h1>
            <p className="mt-1 text-ink-2">
              {data.job.title} · {data.job.company}
              {data.job.url && (
                <a href={data.job.url} target="_blank" rel="noreferrer" className="ml-2 text-brand hover:underline">
                  Posting <Icon name="external" className="inline" />
                </a>
              )}
            </p>
          </div>
          <Button onClick={make} disabled={busy}>
            {busy ? "Preparing… (about 30 s)" : prep ? "Make again" : "Make prep"}
          </Button>
        </header>
      )}
      <ErrorNote message={read.error ?? error} />
      {data === null ? (
        <StateNote>Loading…</StateNote>
      ) : prep === null ? (
        <div className="rounded-lg border border-line bg-panel px-4 py-12 text-center">
          <p className="font-bold">No prep yet for this job</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-ink-2">
            It reads the posting and your Profile summary and facts, then writes likely questions with short answers.
          </p>
        </div>
      ) : (
        <>
          <p className="text-xs text-ink-3">
            Made {istDate(prep.created_at)}. Say the answers in your own words. Anything marked &ldquo;Check this&rdquo;
            is not backed by your profile: fix it or drop it.
          </p>
          <Panel title="Tell me about yourself">
            <Bullets items={prep.pitch} />
          </Panel>
          {KINDS.map(({ kind, title }) => {
            const qs = prep.questions.filter((q) => q.kind === kind);
            return (
              qs.length > 0 && (
                <Panel key={kind} title={title}>
                  <ul className="divide-y divide-line">
                    {qs.map((q, i) => (
                      <Question key={i} q={q} />
                    ))}
                  </ul>
                </Panel>
              )
            );
          })}
          {prep.gaps.length > 0 && (
            <Panel title="Gaps: what to say honestly">
              <ul className="flex flex-col gap-3 text-sm">
                {prep.gaps.map((g, i) => (
                  <li key={i}>
                    <span className="font-semibold">{g.topic}</span>
                    <span className="block text-ink-2">{g.say}</span>
                  </li>
                ))}
              </ul>
            </Panel>
          )}
          {prep.ask_them.length > 0 && (
            <Panel title="Questions to ask them">
              <Bullets items={prep.ask_them} />
            </Panel>
          )}
        </>
      )}
    </div>
  );
}

export default function PrepPage() {
  return (
    <Suspense fallback={<StateNote>Loading…</StateNote>}>
      <JobPrep />
    </Suspense>
  );
}
