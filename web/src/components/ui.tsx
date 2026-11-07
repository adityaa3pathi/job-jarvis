import type { ButtonHTMLAttributes, ReactNode } from "react";

export type Tone = "up" | "down" | "warn" | "plain" | "brand";

const BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-ctl text-sm font-semibold transition-colors disabled:cursor-not-allowed";
const LOOKS = {
  primary: "bg-brand text-brand-ink hover:bg-brand-hover disabled:bg-line-strong disabled:text-ink-3",
  ghost: "border border-line-strong bg-panel text-ink hover:border-ink-3 disabled:text-ink-3 disabled:hover:border-line-strong",
  quiet: "text-ink-2 hover:bg-sunk hover:text-ink disabled:text-ink-3 disabled:hover:bg-transparent",
} as const;
const SIZES = { md: "h-9 px-3.5", sm: "h-8 px-2.5" } as const;

export const buttonClass = (variant: keyof typeof LOOKS = "primary", size: keyof typeof SIZES = "md") =>
  `${BASE} ${LOOKS[variant]} ${SIZES[size]}`;

export function Button({
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof LOOKS; size?: keyof typeof SIZES }) {
  return <button className={`${buttonClass(variant, size)} ${className}`} {...props} />;
}

export const FIELD =
  "h-9 rounded-ctl border border-line-strong bg-panel px-3 text-sm text-ink transition-colors hover:border-ink-3 focus:border-brand focus:outline-none";

/** A titled block of one page; sections sit on hairlines, not on stacked cards. */
export function Panel({
  title,
  aside,
  children,
  className = "",
}: {
  title?: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-line bg-panel ${className}`}>
      {title && (
        <header className="flex items-center gap-3 border-b border-line px-4 py-3">
          <h2 className="text-sm font-bold tracking-tight">{title}</h2>
          {aside && <div className="ml-auto">{aside}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function PageHead({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-2xl font-extrabold tracking-tight">{title}</h1>
      {children}
    </div>
  );
}

export function StateNote({ children }: { children: ReactNode }) {
  return <p className="px-4 py-10 text-center text-sm text-ink-3">{children}</p>;
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="rounded-ctl bg-down-soft px-3 py-2 text-sm font-medium text-down">
      {message}
    </p>
  );
}

/** Quiet note while a cached page checks the server; the live region stays mounted so it is announced. */
export function Refreshing({ on, className = "" }: { on: boolean; className?: string }) {
  return (
    <span role="status" aria-live="polite" className={`text-xs text-ink-3 motion-safe:animate-pulse ${className}`}>
      {on ? "Refreshing…" : ""}
    </span>
  );
}

const TONES: Record<Tone, string> = {
  up: "bg-up-soft text-up",
  down: "bg-down-soft text-down",
  warn: "bg-warn-soft text-warn",
  brand: "bg-brand-soft text-brand",
  plain: "bg-sunk text-ink-2",
};

export function Tag({ tone = "plain", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex h-6 items-center rounded px-2 text-xs font-semibold ${TONES[tone]}`}>{children}</span>
  );
}

export function Chips({ items, tone }: { items: string[]; tone: Tone }) {
  if (!items.length) return <p className="text-sm text-ink-3">None</p>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((i) => (
        <li key={i}>
          <Tag tone={tone}>{i}</Tag>
        </li>
      ))}
    </ul>
  );
}
