"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { api, invalidate, istDate, useCachedApi, type NetworkView } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, Panel, StateNote } from "@/components/ui";

const PATH = "/network";

/** Warm paths: companies in the job list where the owner already knows someone. */
export function NetworkPanel() {
  const { data, error } = useCachedApi<NetworkView>(PATH);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function run(work: () => Promise<string>) {
    setBusy(true);
    setActionError(null);
    try {
      setNote(await work());
      await invalidate(PATH);
      invalidate("/jobs");
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const upload = (file: File) =>
    run(async () => {
      const form = new FormData();
      form.append("file", file);
      const out = await api<{ imported: number }>("/network/import", { method: "POST", body: form });
      return `Imported ${out.imported} connections.`;
    });

  const clear = () =>
    run(async () => {
      if (!window.confirm("Remove every imported and added connection?")) return "Nothing removed.";
      const out = await api<{ removed: number }>("/network", { method: "DELETE" });
      return `Removed ${out.removed} connections.`;
    });

  return (
    <Panel
      title="Your network"
      aside={data && data.people > 0 && <span className="text-xs text-ink-3">{data.people} people · updated {istDate(data.updated)}</span>}
    >
      <div className="flex flex-col gap-3">
        <p className="text-sm text-ink-2">
          Knowing someone at a company makes a hire about 3.6× more likely. Upload LinkedIn&apos;s own export to see where you
          already know someone: LinkedIn → Settings → Data privacy → Get a copy of your data → Connections. The file stays in
          this app and is never sent to the AI.
        </p>
        <div className="flex flex-wrap gap-2">
          <input
            ref={input}
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) upload(file);
              e.target.value = "";
            }}
          />
          <Button variant="ghost" disabled={busy} onClick={() => input.current?.click()}>
            <Icon name="upload" />
            {busy ? "Working…" : "Upload Connections.csv"}
          </Button>
          {data && data.people > 0 && (
            <Button variant="quiet" disabled={busy} onClick={clear}>
              Remove all
            </Button>
          )}
        </div>
        {note && <p className="text-sm text-ink-2">{note}</p>}
        <ErrorNote message={error ?? actionError} />
        {data === null ? (
          <StateNote>Loading…</StateNote>
        ) : data.matches.length === 0 ? (
          data.people > 0 && <p className="text-sm text-ink-3">None of your connections work at a company in your job list yet.</p>
        ) : (
          <ul className="divide-y divide-line rounded-ctl border border-line">
            {data.matches.map((m) => (
              <li key={m.company} className="flex flex-col gap-1.5 px-3 py-2.5">
                <span className="font-bold">
                  {m.company} <span className="text-sm font-normal text-ink-3">· you know {m.people.length}</span>
                </span>
                <span className="text-sm text-ink-2">
                  {m.people.slice(0, 4).map((p, i) => (
                    <span key={`${p.name}-${i}`}>
                      {i > 0 && ", "}
                      {p.url ? (
                        <a href={p.url} target="_blank" rel="noreferrer" className="font-semibold text-brand hover:underline">
                          {p.name}
                        </a>
                      ) : (
                        p.name
                      )}
                      {p.position && ` (${p.position})`}
                    </span>
                  ))}
                  {m.people.length > 4 && ` and ${m.people.length - 4} more`}
                </span>
                <span className="flex flex-wrap gap-x-3 text-sm">
                  {m.jobs.map((j) => (
                    <Link key={j.id} href={`/outreach/${j.id}`} className="text-brand hover:underline">
                      {j.title}
                    </Link>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}
