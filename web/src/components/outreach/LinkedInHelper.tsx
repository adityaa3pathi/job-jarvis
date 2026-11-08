"use client";

import { useState } from "react";
import { post } from "@/lib/api";
import { Button, ErrorNote, Panel } from "@/components/ui";

/** Makes the token the private Chrome extension uses; shown once, a new one replaces it. */
export function LinkedInHelper() {
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function make() {
    if (token === null && !window.confirm("Make a new token? Any extension using the old one stops working.")) return;
    setBusy(true);
    setError(null);
    try {
      setToken((await post<{ token: string }>("/extension/token")).token);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="LinkedIn helper">
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-ink-2">
          A private Chrome extension that fills your edited notes into LinkedIn&apos;s own Connect and Message boxes. You always
          press LinkedIn&apos;s Send; it never sends, connects or likes by itself. Install it from the <code className="rounded bg-sunk px-1">extension</code>{" "}
          folder of this project (Chrome → Extensions → Developer mode → Load unpacked).
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" disabled={busy} onClick={make}>
            {busy ? "Making…" : "Make token"}
          </Button>
          {token && (
            <Button
              variant="quiet"
              onClick={async () => {
                await navigator.clipboard.writeText(token);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy token"}
            </Button>
          )}
        </div>
        {token && (
          <div className="flex flex-col gap-1 rounded-ctl bg-sunk px-3 py-2">
            <span>
              Address: <code>{window.location.origin}</code>
            </span>
            <span className="break-all">
              Token: <code>{token}</code>
            </span>
            <span className="text-xs text-ink-3">Paste both into the extension&apos;s settings. The token is shown only now.</span>
          </div>
        )}
        <ErrorNote message={error} />
      </div>
    </Panel>
  );
}
