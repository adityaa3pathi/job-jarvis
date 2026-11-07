"use client";

import { useEffect, useRef, useState } from "react";
import { api, invalidate, runTask, TaskAborted, useCachedApi, type OutreachOverview, type SenderProfile } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { NetworkPanel } from "@/components/outreach/NetworkPanel";
import { ThreadCard } from "@/components/outreach/ThreadCard";
import { Button, ErrorNote, FIELD, PageHead, Panel, Refreshing, StateNote } from "@/components/ui";

const PATH = "/outreach";

function Setup({ ready }: { ready: OutreachOverview["ready"] }) {
  if (ready.gmail && ready.hunter && ready.apollo) return null;
  return (
    <Panel title="Finish setup">
      <ul className="flex flex-col gap-2 text-sm text-ink-2">
        {!ready.gmail && (
          <li>
            <span className="font-semibold text-ink">Gmail is not connected.</span> Emails can be written but not sent. Run{" "}
            <code className="rounded bg-sunk px-1">job-agent gmail-auth</code> once on your computer, then add GMAIL_CLIENT_ID,
            GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN to the server.
          </li>
        )}
        {!ready.apollo && (
          <li>
            <span className="font-semibold text-ink">Apollo is not set up.</span> Add APOLLO_API_KEY to the server to find
            managers and founders by company website. Sign up with a work email: free accounts on Gmail cannot use the API.
          </li>
        )}
        {!ready.hunter && (
          <li>
            <span className="font-semibold text-ink">Hunter is not set up.</span> Add HUNTER_API_KEY to the server to look up
            work emails (free plan: about 50 a month).
          </li>
        )}
      </ul>
    </Panel>
  );
}

function ProfileForm({ profile, onSaved }: { profile: SenderProfile; onSaved: () => void }) {
  const [me, setMe] = useState({ ...profile, proof: profile.proof ?? "", links: profile.links.join("\n") });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const empty = !profile.name || !profile.proof;
  return (
    <details open={empty} className="rounded-lg border border-line bg-panel">
      <summary className="cursor-pointer px-4 py-3 text-sm font-bold">Your details for every message</summary>
      <form
        className="grid gap-2 border-t border-line p-4"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            const links = me.links.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 4);
            await api("/outreach/profile", { method: "PUT", body: JSON.stringify({ ...me, links }) });
            onSaved();
          } catch (err) {
            setError((err as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <input aria-label="Your name" placeholder="Your name" className={FIELD} value={me.name} onChange={(e) => setMe({ ...me, name: e.target.value })} />
        <textarea
          aria-label="Proof line"
          placeholder="Proof line, link first: e.g. https://docnow.in — I built this live booking platform alone; its AI support agent scores 92–96% on 50 real support questions, graded by hand."
          className={`${FIELD} h-20 py-2`}
          value={me.proof}
          onChange={(e) => setMe({ ...me, proof: e.target.value })}
        />
        <p className="text-xs text-ink-3">
          Every first message opens with this line. Say how you measured the result: hirers say it is the one thing they never get.
        </p>
        <textarea
          aria-label="One more line about you (optional)"
          placeholder="Optional extra line, e.g. 2.5 years building backend and AI systems in TypeScript and Python."
          className={`${FIELD} h-20 py-2`}
          value={me.pitch}
          onChange={(e) => setMe({ ...me, pitch: e.target.value })}
        />
        <textarea
          aria-label="Links, one per line"
          placeholder="Links, one per line (site, GitHub, LinkedIn)"
          className={`${FIELD} h-20 py-2`}
          value={me.links}
          onChange={(e) => setMe({ ...me, links: e.target.value })}
        />
        <Button type="submit" variant="ghost" disabled={busy} className="justify-self-start">
          Save details
        </Button>
        <ErrorNote message={error} />
      </form>
    </details>
  );
}

/** Runs only once data is loaded in the browser: the date must never be read while prerendering. */
function dueToday(items: OutreachOverview["items"]) {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  return items.filter((t) => t.next_due && t.next_due <= today && ["waiting", "accepted"].includes(t.state));
}

export default function OutreachPage() {
  const { data, error, refreshing } = useCachedApi<OutreachOverview>(PATH);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  const refresh = () => invalidate(PATH);

  async function checkReplies() {
    setBusy(true);
    setActionError(null);
    try {
      const got = await runTask<{ checked: number; replied: number; bounced: number; errors: number }>(
        "/outreach/check-replies",
        undefined,
        alive.current?.signal,
      );
      setNote(`Checked ${got.checked} threads: ${got.replied} replied, ${got.bounced} bounced.`);
      await refresh();
    } catch (e) {
      if (!(e instanceof TaskAborted)) setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const due = data ? dueToday(data.items) : [];
  const drafts = data?.items.filter((t) => t.step === 0 && t.state === "draft") ?? [];
  const rest = data?.items.filter((t) => !due.includes(t) && !drafts.includes(t)) ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <PageHead title="Outreach">
        <Refreshing on={refreshing && data !== null} />
        {data?.ready.gmail && (
          <Button variant="ghost" className="ml-auto" disabled={busy} onClick={checkReplies}>
            <Icon name="refresh" />
            {busy ? "Checking…" : "Check replies"}
          </Button>
        )}
      </PageHead>
      <ErrorNote message={error ?? actionError} />
      {note && <p className="text-sm text-ink-2">{note}</p>}
      {data === null ? (
        <StateNote>Loading outreach…</StateNote>
      ) : (
        <>
          <Setup ready={data.ready} />
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              ["First emails today", `${data.today.emails} / ${data.limits.first_emails_per_day}`],
              ["LinkedIn invites today", `${data.today.invites} / ${data.limits.linkedin_invites_per_day}`],
              ["Sent, last 7 days", String(data.stats.sent)],
              ["Replies, last 7 days", `${data.stats.replied}${data.stats.sent ? ` (${Math.round((100 * data.stats.replied) / data.stats.sent)}%)` : ""}`],
            ].map(([k, v]) => (
              <div key={k} className="rounded-lg border border-line bg-panel px-4 py-3">
                <dt className="text-xs text-ink-3">{k}</dt>
                <dd className="figure text-xl font-extrabold">{v}</dd>
              </div>
            ))}
          </dl>
          <ProfileForm key={JSON.stringify(data.profile)} profile={data.profile} onSaved={refresh} />
          <NetworkPanel />
          {data.items.length === 0 ? (
            <StateNote>No outreach yet. Open a job on the Jobs page and press “Reach out”.</StateNote>
          ) : (
            [
              ["Due today", due],
              ["Drafts to finish", drafts],
              ["Everything else", rest],
            ].map(([title, list]) =>
              (list as typeof data.items).length ? (
                <section key={title as string} className="flex flex-col gap-3">
                  <h2 className="text-sm font-bold tracking-tight">{title as string}</h2>
                  {(list as typeof data.items).map((t) => (
                    <ThreadCard key={`${t.id}-${t.step}-${t.state}`} thread={t} onChange={refresh} showJob />
                  ))}
                </section>
              ) : null,
            )
          )}
        </>
      )}
    </div>
  );
}
