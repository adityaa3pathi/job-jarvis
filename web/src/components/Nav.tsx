"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { clearCache, post } from "@/lib/api";
import { Icon } from "@/components/Icon";

const LINKS = [
  { href: "/", label: "Jobs" },
  { href: "/coach", label: "Coach" },
  { href: "/resumes", label: "Resumes" },
  { href: "/profile", label: "Profile" },
];

export function Mark() {
  return (
    <span className="flex items-center gap-2 text-[15px] font-extrabold tracking-tight">
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false">
        <rect width="24" height="24" rx="6" fill="var(--brand)" />
        <path d="M6 15.5 10 11l3 3 5-6" fill="none" stroke="var(--brand-ink)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      Job Agent
    </span>
  );
}

export function Nav() {
  const path = usePathname();
  const [error, setError] = useState<string | null>(null);
  if (path === "/login") return null;
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-panel/90 backdrop-blur supports-[backdrop-filter]:bg-panel/80">
      <nav className="mx-auto flex h-14 max-w-[1360px] items-center gap-1 px-4 sm:px-6">
        <Link href="/" className="mr-3 rounded-ctl sm:mr-6" aria-label="Job Agent, jobs">
          <Mark />
        </Link>
        <ul className="flex h-full items-stretch gap-0.5 overflow-x-auto">
          {LINKS.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
            return (
              <li key={l.href} className="flex">
                <Link
                  href={l.href}
                  aria-current={active ? "page" : undefined}
                  className={`relative flex items-center px-2.5 text-sm font-semibold sm:px-3 ${
                    active
                      ? "text-ink after:absolute after:inset-x-2.5 after:bottom-0 after:h-0.5 after:rounded-full after:bg-brand"
                      : "text-ink-3 hover:text-ink"
                  }`}
                >
                  {l.label}
                </Link>
              </li>
            );
          })}
        </ul>
        {error && (
          <span role="alert" className="ml-auto hidden text-sm text-down sm:inline">
            Log out failed: {error}
          </span>
        )}
        <button
          className={`${error ? "ml-2" : "ml-auto"} flex h-9 items-center gap-1.5 rounded-ctl px-2 text-sm font-semibold text-ink-3 hover:bg-sunk hover:text-ink`}
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
          <Icon name="logout" />
          <span className="hidden sm:inline">Log out</span>
          <span className="sr-only sm:hidden">Log out</span>
        </button>
      </nav>
    </header>
  );
}
