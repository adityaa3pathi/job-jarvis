"use client";

import { Icon } from "@/components/Icon";
import { pageList } from "./filters";

const BTN = "figure flex h-8 min-w-8 items-center justify-center rounded-ctl px-2 text-sm font-semibold transition-colors";

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
        aria-label="Previous page"
        className={`${BTN} text-ink-2 hover:bg-sunk disabled:text-ink-3 disabled:hover:bg-transparent`}
        disabled={page <= 1}
        onClick={() => onStep(-1)}
      >
        <Icon name="arrowLeft" />
      </button>
      {pageList(page, last).map((n, i) =>
        n === "gap" ? (
          <span key={`gap-${i}`} aria-hidden="true" className="px-1 text-ink-3">
            …
          </span>
        ) : (
          <button
            key={n}
            type="button"
            aria-label={`Page ${n}`}
            aria-current={n === page ? "page" : undefined}
            className={`${BTN} ${n === page ? "bg-ink text-bg" : "text-ink-2 hover:bg-sunk"}`}
            onClick={() => n !== page && onPage(n)}
          >
            {n}
          </button>
        ),
      )}
      <button
        type="button"
        aria-label="Next page"
        className={`${BTN} text-ink-2 hover:bg-sunk disabled:text-ink-3 disabled:hover:bg-transparent`}
        disabled={page >= last}
        onClick={() => onStep(1)}
      >
        <Icon name="chevron" />
      </button>
    </nav>
  );
}
