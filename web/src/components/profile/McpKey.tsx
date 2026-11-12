"use client";

import { useState } from "react";
import { api, invalidate, post, useCachedApi } from "@/lib/api";
import { Button, ErrorNote, Panel, Tag } from "@/components/ui";

/** The key the job-jarvis MCP server uses (Claude Code, Antigravity); shown once. */
export function McpKey() {
  const { data } = useCachedApi<{ on: boolean }>("/mcp/key");
  const [key, setKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function run(change: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await change();
      await invalidate("/mcp/key");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const make = () =>
    run(async () => {
      if (data?.on && !window.confirm("Make a new key? The MCP server using the old one stops working.")) return;
      setKey((await post<{ key: string }>("/mcp/key")).key);
      setCopied(false);
    });
  const off = () =>
    run(async () => {
      if (!window.confirm("Turn off the MCP key? Claude and Antigravity lose access until you make a new one.")) return;
      await api("/mcp/key", { method: "DELETE" });
      setKey(null);
    });

  return (
    <Panel title="AI assistants (MCP)" aside={data && <Tag tone={data.on ? "up" : "plain"}>{data.on ? "On" : "Off"}</Tag>}>
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-ink-2">
          Lets Claude Code or Antigravity search your jobs, update the Tracker, and make interview prep and resumes for you. They
          never apply or send messages. The key cannot read or change your profile, outreach or other keys.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" disabled={busy} onClick={make}>
            {busy ? "Working…" : data?.on ? "Make a new key" : "Make key"}
          </Button>
          {key && (
            <Button
              variant="quiet"
              onClick={async () => {
                await navigator.clipboard.writeText(key);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy key"}
            </Button>
          )}
          {data?.on && (
            <Button variant="quiet" disabled={busy} onClick={off}>
              Turn off
            </Button>
          )}
        </div>
        {key && (
          <div className="flex flex-col gap-1 rounded-ctl bg-sunk px-3 py-2">
            <span className="break-all">
              Key: <code>{key}</code>
            </span>
            <span className="text-xs text-ink-3">Shown only now. Give it to the MCP server setup; you can turn it off here any time.</span>
          </div>
        )}
        <ErrorNote message={error} />
      </div>
    </Panel>
  );
}
