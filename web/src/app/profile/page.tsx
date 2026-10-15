"use client";

import { useId, useState } from "react";
import { api, invalidate, istDate, useCachedApi } from "@/lib/api";
import { Icon } from "@/components/Icon";
import { Button, ErrorNote, FIELD, PageHead, Panel, Refreshing } from "@/components/ui";

type Profile = { summary: string; has_tex: boolean; updated_at: string };

/** A real file input styled as a button, so keyboard and screen readers get the native control. */
function FilePick({ label, accept, onPick }: { label: string; accept: string; onPick: (file: File | undefined) => void }) {
  const id = useId();
  return (
    <span>
      <input
        id={id}
        type="file"
        accept={accept}
        className="peer sr-only"
        onChange={(e) => {
          onPick(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      <label
        htmlFor={id}
        className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-ctl border border-line-strong bg-panel px-2.5 text-sm font-semibold hover:border-ink-3 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand"
      >
        <Icon name="upload" />
        {label}
      </label>
    </span>
  );
}

export default function ProfilePage() {
  const { data: profile, error: loadError, refreshing } = useCachedApi<Profile>("/profile");
  const [edited, setEdited] = useState<string | null>(null); // null: the box follows the saved summary
  const summary = edited ?? profile?.summary ?? "";
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /** A .tex upload must not replace unsaved text in the summary box. */
  async function run(action: () => Promise<unknown>, done: string, keepSummary = false) {
    setError(null);
    setNote(null);
    try {
      await action();
      await invalidate("/profile");
      if (!keepSummary) setEdited(null);
      setNote(done);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function upload(path: string, file: File | undefined, done: string, keepSummary = false) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    run(() => api(path, { method: "POST", body: form }), done, keepSummary);
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5">
      <PageHead title="Profile">
        <Refreshing on={refreshing && profile !== null} />
      </PageHead>
      <ErrorNote message={error ?? loadError} />
      {note && (
        <p role="status" className="flex items-center gap-2 rounded-ctl bg-up-soft px-3 py-2 text-sm font-medium text-up">
          <Icon name="check" />
          {note}
        </p>
      )}
      <Panel
        title="Career summary"
        aside={<FilePick label="Upload file" accept=".pdf,.docx,.md,.txt" onPick={(f) => upload("/profile/summary-file", f, "Summary uploaded.")} />}
      >
        <p className="mb-3 text-sm text-ink-2">
          Upload a PDF, DOCX, MD or TXT, or edit the text. Every line here counts as true about you, so keep it honest.
        </p>
        <textarea
          aria-label="Career summary text"
          className={`${FIELD} h-80 w-full py-3 font-mono text-[13px] leading-relaxed`}
          value={summary}
          onChange={(e) => setEdited(e.target.value)}
        />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button
            disabled={edited === null}
            onClick={() =>
              run(() => api("/profile/summary", { method: "PUT", body: JSON.stringify({ summary }) }), "Summary saved.")
            }
          >
            Save summary
          </Button>
          {edited !== null && <span className="text-xs font-semibold text-warn">Unsaved changes</span>}
          {profile?.updated_at && (
            <span className="figure ml-auto text-xs text-ink-3">Last saved {istDate(profile.updated_at)}</span>
          )}
        </div>
      </Panel>
      <Panel
        title="Base resume (.tex)"
        aside={
          <FilePick
            label={profile?.has_tex ? "Replace" : "Upload"}
            accept=".tex"
            onPick={(f) => upload("/profile/tex", f, "Base resume saved.", true)}
          />
        }
      >
        <p className="flex items-center gap-2 text-sm">
          {profile?.has_tex ? (
            <>
              <Icon name="check" className="text-up" />
              A base resume is saved. Upload again to replace it.
            </>
          ) : (
            <span className="text-ink-2">No base resume yet. Upload your LaTeX resume.</span>
          )}
        </p>
      </Panel>
    </div>
  );
}
