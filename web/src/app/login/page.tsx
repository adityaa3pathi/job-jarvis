"use client";

import { useState } from "react";
import { clearCache, post } from "@/lib/api";
import { Mark } from "@/components/Nav";
import { Button, ErrorNote, FIELD } from "@/components/ui";

export default function LoginPage() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/login", { password });
      clearCache();
      window.location.href = "/";
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-[70dvh] items-center justify-center">
      <form onSubmit={submit} className="flex w-full max-w-sm flex-col gap-4 rounded-xl border border-line bg-panel p-6 shadow-sheet">
        <Mark />
        <div>
          <h1 className="text-xl font-extrabold tracking-tight">Log in</h1>
          <p className="mt-1 text-sm text-ink-2">Your jobs, coach and resumes in one place.</p>
        </div>
        <label className="flex flex-col gap-1.5 text-sm font-semibold" htmlFor="pw">
          Password
          <input
            id="pw"
            type="password"
            autoComplete="current-password"
            className={`${FIELD} h-11 font-normal`}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        <ErrorNote message={error} />
        <Button className="h-11" disabled={busy || !password}>
          {busy ? "Checking…" : "Log in"}
        </Button>
      </form>
    </div>
  );
}
