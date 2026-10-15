"use client";

import { useState } from "react";
import { post } from "@/lib/api";
import { Button, ErrorNote } from "@/components/ui";
import { Ornament } from "@/components/Ornament";

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
      window.location.href = "/";
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mx-auto mt-24 flex max-w-sm flex-col gap-3">
      <h1 className="font-display text-xl font-semibold">Job Agent</h1>
      <Ornament />
      <label className="text-sm" htmlFor="pw">
        Password
      </label>
      <input
        id="pw"
        type="password"
        autoComplete="current-password"
        className="rounded border border-border-strong px-3 py-2"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        required
      />
      <ErrorNote message={error} />
      <Button disabled={busy || !password}>{busy ? "Checking…" : "Log in"}</Button>
    </form>
  );
}
