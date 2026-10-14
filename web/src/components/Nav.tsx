"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { post } from "@/lib/api";

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
    <header className="border-b border-zinc-200 bg-white">
      <nav className="mx-auto flex max-w-5xl items-center gap-1 px-4 py-3">
        <span className="mr-4 font-semibold">Job Agent</span>
        {LINKS.map((l) => {
          const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
          return (
            <Link
              key={l.href}
              href={l.href}
              className={`rounded px-3 py-1.5 text-sm ${active ? "bg-zinc-900 text-white" : "text-zinc-700 hover:bg-zinc-100"}`}
            >
              {l.label}
            </Link>
          );
        })}
        {error && (
          <span role="alert" className="ml-auto text-sm text-red-800">
            Log out failed: {error}
          </span>
        )}
        <button
          className={`${error ? "" : "ml-auto "}text-sm text-zinc-500 hover:text-zinc-900`}
          onClick={async () => {
            setError(null);
            try {
              await post("/logout");
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
