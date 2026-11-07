"use client";

import { useEffect, useRef, useState } from "react";
import { runTask, TaskAborted } from "@/lib/api";

/** Builds the PDF as a background task (it can outlast the proxy), then downloads it. */
export function PdfLink({ buildId }: { buildId: number }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    alive.current = ctl;
    return () => ctl.abort();
  }, []);

  async function build() {
    setBusy(true);
    setError(null);
    try {
      await runTask(`/builds/${buildId}/pdf`, undefined, alive.current?.signal);
      const link = document.createElement("a");
      link.href = `/api/builds/${buildId}/pdf`;
      link.download = `resume-${buildId}.pdf`;
      link.click();
    } catch (err) {
      if (err instanceof TaskAborted) return;
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
