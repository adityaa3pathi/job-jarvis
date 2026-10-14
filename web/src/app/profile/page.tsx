"use client";

import { useEffect, useState } from "react";
import { api, istDate } from "@/lib/api";
import { Button, Card, ErrorNote } from "@/components/ui";

type Profile = { summary: string; has_tex: boolean; updated_at: string };

export default function ProfilePage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [summary, setSummary] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function show(p: Profile) {
    setProfile(p);
    setSummary(p.summary);
  }

  /** A .tex upload must not replace unsaved text in the summary box. */
  async function load(keepSummary: boolean) {
    const p = await api<Profile>("/profile");
    if (keepSummary) setProfile(p);
    else show(p);
  }

  useEffect(() => {
    api<Profile>("/profile").then(show, (e: Error) => setError(e.message));
  }, []);

  async function run(action: () => Promise<unknown>, done: string, keepSummary = false) {
    setError(null);
    setNote(null);
    try {
      await action();
      await load(keepSummary);
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
      <h1 className="text-xl font-semibold">Profile</h1>
      <ErrorNote message={error} />
      {note && <p className="text-sm text-green-800">{note}</p>}
      <Card title="Career summary">
        <p className="mb-2 text-sm text-zinc-600">
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
          className="h-72 w-full rounded border border-zinc-300 p-2 font-mono text-sm"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
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
            <span className="text-xs text-zinc-500">Last saved {istDate(profile.updated_at)}</span>
          )}
        </div>
      </Card>
      <Card title="Base resume (.tex)">
        <p className="mb-2 text-sm text-zinc-600">
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
