"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { clearCache, post } from "@/lib/api";
import { ThemeSwitch } from "@/components/ThemeSwitch";

const LINKS = [
  { href: "/", label: "Jobs" },
  { href: "/coach", label: "Coach" },
  { href: "/resumes", label: "Resumes" },
  { href: "/profile", label: "Profile" },
];

export function Nav() {
  const path = usePathname();
  const [error, setError] = useState<string | null>(null);
  if (path === "/login") return null;
  return (
    <header className="site-header border-b border-border bg-surface-raised">
      <nav className="mx-auto flex max-w-5xl flex-wrap items-center gap-1 px-4 py-3">
        <span className="mr-4 font-display font-semibold">Job Agent</span>
        {LINKS.map((l) => {
          const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
          return (
            <Link
              key={l.href}
              href={l.href}
              className={`rounded px-3 py-1.5 text-sm ${active ? "bg-accent text-accent-ink" : "text-ink-muted hover:bg-surface-muted"}`}
            >
              {l.label}
            </Link>
          );
        })}
        {error && (
          <span role="alert" className="ml-auto text-sm text-danger">
            Log out failed: {error}
          </span>
        )}
        <span className={error ? "" : "ml-auto"}>
          <ThemeSwitch />
        </span>
        <button
          className="text-sm text-ink-faint hover:text-ink"
          onClick={async () => {
            setError(null);
            try {
              await post("/logout");
              clearCache();
              window.location.href = "/login";
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          Log out
        </button>
      </nav>
    </header>
  );
}
