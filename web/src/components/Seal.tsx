import type { ReactNode } from "react";

export type Tone = "good" | "warn" | "bad" | "plain";

const TONES: Record<Tone, string> = {
  good: "bg-success-surface text-success",
  warn: "bg-warning-surface text-warning",
  bad: "bg-danger-surface text-danger",
  plain: "bg-surface-muted text-ink",
};

/** A status badge: a flat chip in plain, a small wax seal in parchment. */
export function Seal({ tone = "plain", className = "", children }: { tone?: Tone; className?: string; children: ReactNode }) {
  return <span className={`seal rounded ${TONES[tone]} ${className}`}>{children}</span>;
}

/** The fit score: bold text in plain, a round wax medallion in parchment. */
export function Medal({ children }: { children: ReactNode }) {
  return <span className="seal-medal text-3xl font-semibold">{children}</span>;
}
