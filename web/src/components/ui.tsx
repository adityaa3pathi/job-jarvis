import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" }) {
  const look =
    variant === "primary"
      ? "bg-zinc-900 text-white hover:bg-zinc-700 disabled:bg-zinc-400"
      : "border border-zinc-300 text-zinc-800 hover:bg-zinc-100 disabled:text-zinc-400";
  return <button className={`rounded px-3 py-1.5 text-sm ${look} ${className}`} {...props} />;
}

export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      {title && <h2 className="mb-2 font-semibold">{title}</h2>}
      {children}
    </section>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {message}
    </p>
  );
}

export function Chips({ items, tone }: { items: string[]; tone: "good" | "warn" | "bad" | "plain" }) {
  if (!items.length) return <p className="text-sm text-zinc-500">None</p>;
  const colors = {
    good: "bg-green-100 text-green-900",
    warn: "bg-amber-100 text-amber-900",
    bad: "bg-red-100 text-red-900",
    plain: "bg-zinc-100 text-zinc-800",
  }[tone];
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((i) => (
        <li key={i} className={`rounded px-2 py-0.5 text-sm ${colors}`}>
          {i}
        </li>
      ))}
    </ul>
  );
}
