"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { post } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Jobs" },
  { href: "/coach", label: "Coach" },
  { href: "/resumes", label: "Resumes" },
  { href: "/profile", label: "Profile" },
];

export function Nav() {
  const path = usePathname();
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
        <button
          className="ml-auto text-sm text-zinc-500 hover:text-zinc-900"
          onClick={async () => {
            await post("/logout");
            window.location.href = "/login";
          }}
        >
          Log out
        </button>
      </nav>
    </header>
  );
}
