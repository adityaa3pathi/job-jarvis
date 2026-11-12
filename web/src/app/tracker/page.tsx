"use client";

import Link from "next/link";
import { useState } from "react";
import { STAGES, invalidate, istDate, patch, post, useCachedApi, type Application, type Stage, type TrackerView } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, FIELD, PageHead, Refreshing, StateNote, Tag } from "@/components/ui";

const STAGE_LABELS: Record<Stage, string> = {
  applied: "Applied",
  replied: "Replied",
  interview: "Interview",
  offer: "Offer",
  rejected: "Rejected",
};

const IST_PARTS = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Kolkata",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** A stored time as the `datetime-local` value it shows in IST, e.g. "2026-11-20T15:30". */
function toLocalIst(value: string | null): string {
  if (!value) return "";
  const p = Object.fromEntries(IST_PARTS.formatToParts(new Date(value)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

function waitLabel(a: Application): string {
  const since = a.applied_at ? `Applied ${istDate(a.applied_at)}` : "";
  const days = a.days_waiting;
  if (a.status !== "applied" || days === null) return since;
  const span = days === 0 ? "today" : days === 1 ? "1 day ago" : `${days} days ago`;
  if (a.followed_up_at) return `${since} · followed up ${span}`;
  return days === 0 ? since : `${since} · no reply for ${days === 1 ? "1 day" : `${days} days`}`;
}

function Row({ a, onError }: { a: Application; onError: (m: string | null) => void }) {
  const [note, setNote] = useState(a.note);
  const [busy, setBusy] = useState(false);

  async function save(change: () => Promise<unknown>) {
    onError(null);
    setBusy(true);
    try {
      await change();
      await invalidate("/tracker");
      invalidate("/jobs");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-col gap-3 px-4 py-4">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <span className="min-w-0 flex-1 basis-64">
          <span className="flex items-center gap-2">
            {a.url ? (
              <a href={a.url} target="_blank" rel="noreferrer" className="truncate font-bold hover:underline">
                {a.title}
              </a>
            ) : (
              <span className="truncate font-bold">{a.title}</span>
            )}
            {a.follow_up_due && <Tag tone="warn">Follow up</Tag>}
          </span>
          <span className="block truncate text-sm text-ink-2">{[a.company, a.city || a.location].filter(Boolean).join(" · ")}</span>
          <span className="block text-xs text-ink-3">{waitLabel(a)}</span>
        </span>
        <label className="flex items-center gap-2 text-sm">
          <span className="sr-only">Stage of {a.title}</span>
          <select
            className={FIELD}
            value={a.status}
            disabled={busy}
            onChange={(e) => save(() => post(`/jobs/${a.id}/mark`, { status: e.target.value }))}
          >
            {STAGES.map((s) => (
              <option key={s} value={s}>
                {STAGE_LABELS[s]}
              </option>
            ))}
            <option value="found">Back to job list</option>
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        {a.status === "interview" && (
          <label className="flex flex-col gap-1 text-xs font-semibold text-ink-3">
            Interview (IST)
            <input
              type="datetime-local"
              className={FIELD}
              defaultValue={toLocalIst(a.interview_at)}
              disabled={busy}
              onBlur={(e) => {
                const v = e.target.value;
                if (v === toLocalIst(a.interview_at)) return;
                save(() => patch(`/tracker/${a.id}`, { interview_at: v ? `${v}:00+05:30` : null }));
              }}
            />
          </label>
        )}
        <label className="flex min-w-60 flex-1 flex-col gap-1 text-xs font-semibold text-ink-3">
          Notes
          <textarea
            className={`${FIELD} h-auto min-h-9 py-2 font-normal`}
            rows={1}
            maxLength={4000}
            value={note}
            disabled={busy}
            placeholder="Recruiter name, salary talk, next step…"
            onChange={(e) => setNote(e.target.value)}
            onBlur={() => note !== a.note && save(() => patch(`/tracker/${a.id}`, { note }))}
          />
        </label>
        {a.follow_up_due && (
          <Button variant="ghost" disabled={busy} onClick={() => save(() => patch(`/tracker/${a.id}`, { followed_up: true }))}>
            <Icon name="check" />I followed up
          </Button>
        )}
        <Link href={`/prep/${a.id}`} className="text-sm font-semibold text-brand hover:underline">
          Interview prep
        </Link>
      </div>
    </li>
  );
}

export default function TrackerPage() {
  const { data, error, refreshing } = useCachedApi<TrackerView>("/tracker");
  const [stage, setStage] = useState<Stage | "all">("all");
  const [saveError, setSaveError] = useState<string | null>(null);
  const items = (data?.items ?? []).filter((a) => stage === "all" || a.status === stage);
  const due = data?.items.filter((a) => a.follow_up_due).length ?? 0;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <PageHead title="Tracker">
        <Refreshing on={refreshing && data !== null} />
        {due > 0 && <Tag tone="warn">{due} to follow up</Tag>}
      </PageHead>
      <div role="tablist" aria-label="Stage" className="flex flex-wrap gap-1">
        {(["all", ...STAGES] as const).map((s) => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={stage === s}
            onClick={() => setStage(s)}
            className={`h-8 rounded-ctl px-3 text-sm font-semibold ${stage === s ? "bg-ink text-panel" : "text-ink-2 hover:bg-sunk"}`}
          >
            {s === "all" ? "All" : STAGE_LABELS[s]}
            {data && <span className="figure ml-1.5 opacity-70">{s === "all" ? data.items.length : data.counts[s]}</span>}
          </button>
        ))}
      </div>
      <ErrorNote message={error ?? saveError} />
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        {data === null ? (
          <StateNote>Loading applications…</StateNote>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-3 px-4 py-12 text-center">
            <p className="font-bold">{stage === "all" ? "No applications yet" : `Nothing in ${STAGE_LABELS[stage]}`}</p>
            <p className="max-w-sm text-sm text-ink-2">Mark a job Applied on the Jobs page and it shows up here.</p>
          </div>
        ) : (
          <ul className="divide-y divide-line">
            {items.map((a) => (
              <Row key={`${a.id}-${a.note}`} a={a} onError={setSaveError} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
