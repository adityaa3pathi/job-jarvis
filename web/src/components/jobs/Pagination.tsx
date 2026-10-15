"use client";

import { pageList } from "./filters";

const BTN = "min-w-9 rounded border px-2.5 py-1 text-sm";

// Prev/Next step from the newest requested page, which may be ahead of the one shown.
type Props = {
  page: number;
  last: number;
  onPage: (page: number) => void;
  onStep: (delta: number) => void;
};

export function Pagination({ page, last, onPage, onStep }: Props) {
  if (last <= 1) return null;
  return (
    <nav aria-label="Pagination" className="flex flex-wrap items-center gap-1">
      <button
        type="button"
        className={`${BTN} border-zinc-300 hover:bg-zinc-100 disabled:text-zinc-400 disabled:hover:bg-transparent`}
        disabled={page <= 1}
        onClick={() => onStep(-1)}
      >
        Prev
      </button>
      {pageList(page, last).map((n, i) =>
        n === "gap" ? (
          <span key={`gap-${i}`} aria-hidden="true" className="px-1 text-zinc-500">
            …
          </span>
        ) : (
          <button
            key={n}
            type="button"
            aria-label={`Page ${n}`}
            aria-current={n === page ? "page" : undefined}
            className={`${BTN} ${n === page ? "border-zinc-900 bg-zinc-900 text-white" : "border-zinc-300 hover:bg-zinc-100"}`}
            onClick={() => n !== page && onPage(n)}
          >
            {n}
          </button>
        ),
      )}
      <button
        type="button"
        className={`${BTN} border-zinc-300 hover:bg-zinc-100 disabled:text-zinc-400 disabled:hover:bg-transparent`}
        disabled={page >= last}
        onClick={() => onStep(1)}
      >
        Next
      </button>
    </nav>
  );
}
