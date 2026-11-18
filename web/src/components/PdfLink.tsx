"use client";

import { useState } from "react";
import { useTask } from "@/lib/api";

/** Builds the PDF as a background task (it can outlast the proxy), then downloads it. */
export function PdfLink({ buildId }: { buildId: number }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const runTask = useTask();

  async function build() {
    setBusy(true);
    setError(null);
    try {
      await runTask(`/builds/${buildId}/pdf`);
      const link = document.createElement("a");
      link.href = `/api/builds/${buildId}/pdf`;
      link.download = `resume-${buildId}.pdf`;
      link.click();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-col">
      <button type="button" className="text-left font-semibold text-brand hover:underline disabled:text-ink-3" onClick={build} disabled={busy}>
        {busy ? "Building PDF…" : "PDF"}
      </button>
      {error && (
        <span role="alert" className="text-xs text-down">
          {error}
        </span>
      )}
    </span>
  );
}
