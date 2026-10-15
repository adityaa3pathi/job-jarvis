"use client";

import { useSyncExternalStore } from "react";

const YEAR_SECONDS = 60 * 60 * 24 * 365;

function subscribe(onChange: () => void) {
  const watch = new MutationObserver(onChange);
  watch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => watch.disconnect();
}

const isParchment = () => document.documentElement.dataset.theme === "parchment";

/** Toggles the parchment theme; the cookie lets the head script apply it before paint. */
export function ThemeSwitch() {
  // The server cannot know the cookie, so it renders "off"; the client corrects after hydration.
  const on = useSyncExternalStore(subscribe, isParchment, () => false);
  return (
    <button
      type="button"
      aria-pressed={on}
      className="rounded px-2 py-1 text-sm text-ink-muted hover:bg-surface-muted hover:text-ink"
      onClick={() => {
        const next = on ? "plain" : "parchment";
        document.cookie = `theme=${next}; Path=/; Max-Age=${YEAR_SECONDS}; SameSite=Lax`;
        document.documentElement.dataset.theme = next;
      }}
    >
      <span aria-hidden="true">{on ? "✦ " : "✧ "}</span>
      Parchment theme
    </button>
  );
}
