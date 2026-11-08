"use client";

import Link from "next/link";
import { useState } from "react";
import { api, istDate, post, type Thread } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, FIELD, Tag, buttonClass, type Tone } from "@/components/ui";

const NOTE_MAX = 300; // LinkedIn's limit for a connection note

const STATE: Record<Thread["state"], { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "plain" },
  waiting: { label: "Waiting", tone: "brand" },
  accepted: { label: "Accepted", tone: "up" },
  replied: { label: "Replied", tone: "up" },
  bounced: { label: "Bounced", tone: "down" },
  no_reply: { label: "No reply", tone: "plain" },
  stopped: { label: "Stopped", tone: "plain" },
};

function stepName(t: Thread): string {
  if (t.channel === "linkedin") return ["Connection note", "Message after they accept", "LinkedIn follow-up"][t.step] ?? "LinkedIn message";
  return t.step === 0 ? "First email" : `Follow-up ${t.step} of ${t.max_steps - 1}`;
}

/** One person on one channel: edit the next message, send it (email) or mark it sent (LinkedIn). */
export function ThreadCard({ thread: t, onChange, showJob = false }: { thread: Thread; onChange: () => void; showJob?: boolean }) {
  const [subject, setSubject] = useState(t.subject);
  const [body, setBody] = useState(t.body);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const dirty = subject !== t.subject || body !== t.body;
  const note = t.channel === "linkedin" && t.step === 0;
  const tooLong = note && body.length > NOTE_MAX;
  const waitingAccept = t.channel === "linkedin" && t.step === 1 && t.state === "waiting";
  const closed = ["replied", "bounced", "no_reply", "stopped"].includes(t.state);

  async function act(work: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await work();
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const save = () => api(`/outreach/${t.id}`, { method: "PUT", body: JSON.stringify({ subject, body }) });

  async function copy() {
    try {
      await navigator.clipboard.writeText(body);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Could not copy; select the text and copy it yourself.");
    }
  }

  return (
    <article className="flex flex-col gap-3 rounded-lg border border-line bg-panel p-4">
      <header className="flex flex-wrap items-center gap-2">
        <Tag tone={t.channel === "email" ? "brand" : "plain"}>{t.channel === "email" ? "Email" : "LinkedIn"}</Tag>
        <span className="font-bold">{t.name || t.email || "Unnamed contact"}</span>
        {t.title && <span className="text-sm text-ink-2">{t.title}</span>}
        <span className="ml-auto">
          <Tag tone={STATE[t.state].tone}>{STATE[t.state].label}</Tag>
        </span>
      </header>
      <p className="text-sm text-ink-2">
        {showJob && (
          <>
            <Link href={`/outreach/${t.job_id}`} className="font-semibold text-brand hover:underline">
              {t.job_title || "Job"} · {t.company}
            </Link>
            {" · "}
          </>
        )}
        {t.channel === "email" ? t.email : "Send it on LinkedIn yourself"}
        {t.first_sent_at && ` · first sent ${istDate(t.first_sent_at)}`}
        {t.next_due && !closed && ` · next: ${t.todo || "follow-up"} by ${istDate(t.next_due)}`}
      </p>

      {t.facts.length > 0 && t.can_send && t.step === 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-ink-2">Checked facts the draft uses ({t.facts.length})</summary>
          <ul className="mt-2 flex flex-col gap-1.5">
            {t.facts.map((f) => (
              <li key={f.quote}>
                <span className="font-semibold">{f.fact}</span>
                <span className="block text-xs text-ink-3">
                  {f.page ? (
                    <a href={f.page} target="_blank" rel="noreferrer" className="hover:underline">
                      {f.page.replace(/^https:\/\//, "")}
                    </a>
                  ) : f.from === "job" ? (
                    "job post"
                  ) : (
                    "company site"
                  )}
                  : “{f.quote}”
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {t.can_send && !closed && (
        <>
          <p className="text-xs font-bold uppercase tracking-wide text-ink-3">{stepName(t)}</p>
          {t.channel === "email" && t.step === 0 && (
            <input aria-label="Subject" className={FIELD} value={subject} onChange={(e) => setSubject(e.target.value)} />
          )}
          <textarea
            aria-label={stepName(t)}
            className={`${FIELD} ${note ? "h-32" : "h-56"} w-full py-2.5 leading-relaxed`}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
          <p className={`text-xs ${tooLong ? "font-semibold text-down" : "text-ink-3"}`}>
            {note ? `${body.length} of ${NOTE_MAX} characters` : `${body.split(/\s+/).filter(Boolean).length} words`}
            {!t.edited && !dirty && " · rewrite this in your own words before sending"}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button variant="ghost" disabled={busy || !dirty || tooLong} onClick={() => act(save)}>
              Save
            </Button>
            {t.channel === "email" ? (
              <Button
                disabled={busy || tooLong || waitingAccept || (!t.edited && !dirty)}
                onClick={() =>
                  act(async () => {
                    if (dirty) await save();
                    if (!window.confirm(`Send this email to ${t.email} now?`)) return;
                    await post(`/outreach/${t.id}/send`);
                  })
                }
              >
                <Icon name="send" />
                Send from Gmail
              </Button>
            ) : (
              <>
                <Button variant="ghost" disabled={busy} onClick={copy}>
                  {copied ? "Copied" : "Copy"}
                </Button>
                <a href={t.linkedin_url || t.linkedin_search} target="_blank" rel="noreferrer" className={buttonClass("ghost")}>
                  {t.linkedin_url ? "Open profile" : "Find on LinkedIn"}
                  <Icon name="external" />
                </a>
                <Button
                  disabled={busy || tooLong || waitingAccept || (!t.edited && !dirty)}
                  onClick={() =>
                    act(async () => {
                      if (dirty) await save();
                      await post(`/outreach/${t.id}/send`);
                    })
                  }
                >
                  <Icon name="check" />
                  {t.step === 0 ? "I sent the invite" : t.step === 1 ? "I sent the message" : "I sent the follow-up"}
                </Button>
              </>
            )}
          </div>
        </>
      )}

      {!closed && t.step > 0 && (
        <div className="flex flex-wrap gap-2 border-t border-line pt-3">
          {waitingAccept && (
            <Button size="sm" disabled={busy} onClick={() => act(() => post(`/outreach/${t.id}/state`, { state: "accepted" }))}>
              They accepted
            </Button>
          )}
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => act(() => post(`/outreach/${t.id}/state`, { state: "replied" }))}>
            They replied
          </Button>
          <Button size="sm" variant="quiet" disabled={busy} onClick={() => act(() => post(`/outreach/${t.id}/state`, { state: "no_reply" }))}>
            No reply
          </Button>
          <Button size="sm" variant="quiet" disabled={busy} onClick={() => act(() => post(`/outreach/${t.id}/state`, { state: "stopped" }))}>
            Stop
          </Button>
        </div>
      )}
      <ErrorNote message={error} />
    </article>
  );
}
