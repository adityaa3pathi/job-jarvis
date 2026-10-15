"use client";

import { useState } from "react";
import { api, invalidate, istDate, useCachedApi } from "@/lib/api";
import { Button, Card, ErrorNote, Refreshing } from "@/components/ui";
import { Ornament } from "@/components/Ornament";

type Profile = { summary: string; has_tex: boolean; updated_at: string };

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
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-xl font-semibold">Profile</h1>
        <Refreshing on={refreshing && profile !== null} />
      </div>
      <Ornament />
      <ErrorNote message={error ?? loadError} />
      {note && <p className="text-sm text-success">{note}</p>}
      <Card title="Career summary">
        <p className="mb-2 text-sm text-ink-muted">
          Upload a PDF, DOCX, MD or TXT, or edit the text. Every line here counts as true about you, so keep it honest.
        </p>
        <input
          type="file"
          accept=".pdf,.docx,.md,.txt"
          aria-label="Upload career summary"
          className="mb-2 block text-sm"
          onChange={(e) => upload("/profile/summary-file", e.target.files?.[0], "Summary uploaded.")}
        />
        <textarea
          aria-label="Career summary text"
          className="h-72 w-full rounded border border-border-strong p-2 font-mono text-sm"
          value={summary}
          onChange={(e) => setEdited(e.target.value)}
        />
        <div className="mt-2 flex items-center gap-3">
          <Button
            onClick={() =>
              run(() => api("/profile/summary", { method: "PUT", body: JSON.stringify({ summary }) }), "Summary saved.")
            }
          >
            Save summary
          </Button>
          {profile?.updated_at && (
            <span className="text-xs text-ink-faint">Last saved {istDate(profile.updated_at)}</span>
          )}
        </div>
      </Card>
      <Card title="Base resume (.tex)">
        <p className="mb-2 text-sm text-ink-muted">
          {profile?.has_tex ? "A base resume is saved. Upload again to replace it." : "No base resume yet. Upload your LaTeX resume."}
        </p>
        <input
          type="file"
          accept=".tex"
          aria-label="Upload base resume"
          className="block text-sm"
          onChange={(e) => upload("/profile/tex", e.target.files?.[0], "Base resume saved.", true)}
        />
      </Card>
    </div>
  );
}
