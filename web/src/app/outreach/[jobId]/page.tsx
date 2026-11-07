"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { api, invalidate, post, runTask, TaskAborted, useCachedApi, type Candidate, type JobOutreach } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { ThreadCard } from "@/components/outreach/ThreadCard";
import { Button, ErrorNote, FIELD, Panel, Refreshing, StateNote, Tag, buttonClass } from "@/components/ui";

type Found = { domain: string; candidates: Candidate[]; notes: string[] };

const SOURCE: Record<string, string> = {
  post: "in the job post",
  site: "company website",
  yc: "YC founder",
  hunter: "Hunter",
  manual: "added by you",
};

function JobOutreach() {
  const { jobId } = useParams<{ jobId: string }>();
  const path = `/jobs/${jobId}/outreach`;
  const read = useCachedApi<JobOutreach>(path);
  const data = read.key === path ? read.data : null;
  const [domain, setDomain] = useState<string | null>(null);
  const [useHunter, setUseHunter] = useState(false);
  const [found, setFound] = useState<Found | null>(null);
  const [manual, setManual] = useState({ name: "", title: "", email: "", linkedin_url: "" });
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  const site = domain ?? data?.domain_hint ?? "";
  const refresh = () => invalidate(path);

  async function run(label: string, work: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      await work();
    } catch (e) {
      if (!(e instanceof TaskAborted)) setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const find = () =>
    run("find", async () => {
      const got = await runTask<Found>(`/jobs/${jobId}/contacts/find`, { domain: site, use_hunter: useHunter }, alive.current?.signal);
      setFound(got);
      if (!domain && got.domain) setDomain(got.domain);
    });

  const save = (c: Omit<Candidate, "other_emails" | "note" | "source">, after?: () => void) =>
    run(`save-${c.email || c.name}`, async () => {
      await post(`/jobs/${jobId}/contacts`, c);
      after?.();
      await refresh();
    });

  const write = (contactId: number, channel: "email" | "linkedin") =>
    run(`write-${contactId}-${channel}`, async () => {
      await runTask(`/contacts/${contactId}/outreach`, { job_id: Number(jobId), channel, domain: site }, alive.current?.signal);
      await refresh();
      invalidate("/outreach");
    });

  if (!data) {
    return (
      <div className="mx-auto max-w-4xl">
        <ErrorNote message={read.error} />
        {!read.error && <StateNote>Loading…</StateNote>}
      </div>
    );
  }
  const full = data.contacts.length >= data.limit;
  const saved = new Set(data.contacts.flatMap((c) => [c.email, c.name.toLowerCase()]).filter(Boolean));

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <div>
        <Link href="/outreach" className="inline-flex items-center gap-1 text-sm font-semibold text-ink-3 hover:text-ink">
          <Icon name="arrowLeft" />
          Outreach
        </Link>
        <h1 className="mt-1 text-2xl font-extrabold tracking-tight">{data.job.title}</h1>
        <p className="text-ink-2">
          {data.job.company}
          {data.job.urls[0] && (
            <>
              {" · "}
              <a href={data.job.urls[0]} target="_blank" rel="noreferrer" className="font-semibold text-brand hover:underline">
                Open posting
              </a>
            </>
          )}
          <Refreshing on={read.refreshing} className="ml-3" />
        </p>
      </div>
      <ErrorNote message={error} />

      <Panel title={`People at ${data.job.company}`} aside={<span className="text-xs text-ink-3">{data.contacts.length} of {data.limit} saved</span>}>
        {data.contacts.length === 0 ? (
          <p className="text-sm text-ink-2">No one saved yet. Find people below and save one manager, plus one engineer or recruiter.</p>
        ) : (
          <ul className="divide-y divide-line">
            {data.contacts.map((c) => {
              const hasEmail = data.threads.some((t) => t.contact_id === c.id && t.channel === "email");
              const hasNote = data.threads.some((t) => t.contact_id === c.id && t.channel === "linkedin");
              return (
                <li key={c.id} className="flex flex-wrap items-center gap-2 py-2.5">
                  <span className="min-w-0 flex-1 basis-56">
                    <span className="block font-bold">{c.name || c.email}</span>
                    <span className="block truncate text-sm text-ink-2">{[c.title, c.email].filter(Boolean).join(" · ")}</span>
                  </span>
                  {c.email && !hasEmail && (
                    <Button size="sm" disabled={busy !== null} onClick={() => write(c.id, "email")}>
                      {busy === `write-${c.id}-email` ? "Writing…" : "Write email"}
                    </Button>
                  )}
                  {!hasNote && (
                    <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => write(c.id, "linkedin")}>
                      {busy === `write-${c.id}-linkedin` ? "Writing…" : "Write LinkedIn note"}
                    </Button>
                  )}
                  {!hasEmail && !hasNote && (
                    <Button
                      size="sm"
                      variant="quiet"
                      disabled={busy !== null}
                      onClick={() => run(`del-${c.id}`, async () => {
                        await api(`/contacts/${c.id}`, { method: "DELETE" });
                        await refresh();
                      })}
                    >
                      Remove
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Panel>

      {data.threads.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="text-sm font-bold tracking-tight">Messages</h2>
          {data.threads.map((t) => (
            <ThreadCard key={`${t.id}-${t.step}-${t.state}`} thread={t} onChange={refresh} />
          ))}
        </section>
      )}

      {!full && (
        <Panel title="Find people">
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <input
                aria-label="Company website"
                placeholder="Company website, e.g. acme.com"
                className={`${FIELD} min-w-0 flex-1 basis-56`}
                value={site}
                onChange={(e) => setDomain(e.target.value)}
              />
              <label className={`flex items-center gap-2 text-sm ${data.ready.hunter ? "" : "text-ink-3"}`}>
                <input type="checkbox" disabled={!data.ready.hunter} checked={useHunter} onChange={(e) => setUseHunter(e.target.checked)} />
                Also ask Hunter (uses 1 credit)
              </label>
              <Button disabled={busy !== null} onClick={find}>
                <Icon name="search" />
                {busy === "find" ? "Looking…" : "Find people"}
              </Button>
            </div>
            <p className="text-xs text-ink-3">
              Looks in the job post, the company website and YC. Never on LinkedIn.{" "}
              <a href={data.linkedin_search} target="_blank" rel="noreferrer" className="font-semibold text-brand hover:underline">
                Search LinkedIn yourself
              </a>{" "}
              and add the person below.
            </p>
            {found && (
              <>
                {found.notes.map((n) => (
                  <p key={n} className="text-sm text-warn">{n}</p>
                ))}
                {found.candidates.length === 0 ? (
                  <p className="text-sm text-ink-2">Nobody found. Try the company website, Hunter, or add someone by hand.</p>
                ) : (
                  <ul className="divide-y divide-line rounded-ctl border border-line">
                    {found.candidates.map((c) => {
                      const key = c.email || c.name;
                      const done = saved.has(c.email) || saved.has(c.name.toLowerCase());
                      return (
                        <li key={key} className="flex flex-wrap items-center gap-2 px-3 py-2.5">
                          <span className="min-w-0 flex-1 basis-56">
                            <span className="block font-semibold">{c.name || c.email}</span>
                            <span className="block truncate text-sm text-ink-2">{[c.title, c.name ? c.email : ""].filter(Boolean).join(" · ")}</span>
                            <span className="block text-xs text-ink-3">{SOURCE[c.source] ?? c.source} · {c.note}</span>
                          </span>
                          {c.linkedin_url && (
                            <a href={c.linkedin_url} target="_blank" rel="noreferrer" className={buttonClass("quiet", "sm")}>
                              Profile
                              <Icon name="external" />
                            </a>
                          )}
                          {done ? (
                            <Tag tone="up">Saved</Tag>
                          ) : (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={busy !== null}
                              onClick={() => save({ name: c.name, title: c.title, email: c.email, email_source: c.email_source, linkedin_url: c.linkedin_url })}
                            >
                              Save
                            </Button>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </>
            )}
            <form
              className="grid gap-2 border-t border-line pt-3 sm:grid-cols-2"
              onSubmit={(e) => {
                e.preventDefault();
                save({ ...manual, email_source: "manual" }, () => setManual({ name: "", title: "", email: "", linkedin_url: "" }));
              }}
            >
              <p className="text-sm font-semibold sm:col-span-2">Add someone by hand</p>
              <input aria-label="Name" placeholder="Name" className={FIELD} value={manual.name} onChange={(e) => setManual({ ...manual, name: e.target.value })} />
              <input aria-label="Job title" placeholder="Their job title" className={FIELD} value={manual.title} onChange={(e) => setManual({ ...manual, title: e.target.value })} />
              <input aria-label="Email" placeholder="Work email (optional)" className={FIELD} value={manual.email} onChange={(e) => setManual({ ...manual, email: e.target.value })} />
              <input aria-label="LinkedIn profile" placeholder="LinkedIn profile link (optional)" className={FIELD} value={manual.linkedin_url} onChange={(e) => setManual({ ...manual, linkedin_url: e.target.value })} />
              <Button type="submit" variant="ghost" disabled={busy !== null || !(manual.name || manual.email)} className="sm:col-span-2 sm:justify-self-start">
                Save person
              </Button>
            </form>
          </div>
        </Panel>
      )}
    </div>
  );
}

export default function JobOutreachPage() {
  return (
    <Suspense fallback={<StateNote>Loading…</StateNote>}>
      <JobOutreach />
    </Suspense>
  );
}
