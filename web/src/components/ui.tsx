import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Ornament } from "@/components/Ornament";
import { Seal, type Tone } from "@/components/Seal";

export const PRIMARY = "btn-primary bg-accent text-accent-ink hover:bg-accent-hover disabled:bg-accent-disabled";

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" }) {
  const look =
    variant === "primary"
      ? PRIMARY
      : "btn-ghost border border-border-strong text-ink hover:bg-surface-muted disabled:text-ink-faint";
  return <button className={`rounded px-3 py-1.5 text-sm ${look} ${className}`} {...props} />;
}

export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="card rounded-lg border border-border bg-surface-raised p-4">
      {title && <h2 className="mb-2 font-semibold">{title}</h2>}
      {children}
    </section>
  );
}

/** Loading or empty note: today's wording in plain, a short storybook line in parchment. */
export function StateNote({ children, story }: { children: ReactNode; story: string }) {
  return (
    <p className="text-sm text-ink-faint">
      <span className="theme-plain">{children}</span>
      <span className="theme-parchment">
        <Ornament kind="quill" />
        {story}
      </span>
    </p>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="error-note rounded border border-danger-border bg-danger-surface px-3 py-2 text-sm text-danger">
      <span className="theme-parchment">A blot on the page: </span>
      {message}
    </p>
  );
}

export function Chips({ items, tone }: { items: string[]; tone: Tone }) {
  if (!items.length) return <p className="text-sm text-ink-faint">None</p>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((i) => (
        <li key={i}>
          <Seal tone={tone} className="inline-block px-2 py-0.5 text-sm">
            {i}
          </Seal>
        </li>
      ))}
    </ul>
  );
}
