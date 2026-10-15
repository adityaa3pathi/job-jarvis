import type { SVGProps } from "react";

// One stroke family: 24px grid, 1.75 stroke, round joins.
const PATHS = {
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm9 2-4.2-4.2",
  filter: "M4 6h16M7 12h10M10 18h4",
  check: "m5 12.5 4.5 4.5L19 7.5",
  x: "M6 6l12 12M18 6 6 18",
  chat: "M5 18.5V7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H8.5L5 18.5Z",
  external: "M14 5h5v5M19 5l-8 8M17 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h4",
  refresh: "M19 12a7 7 0 1 1-2.05-4.95M19 5v4h-4",
  undo: "M9 7 5 11l4 4M5 11h9a5 5 0 0 1 0 10h-2",
  logout: "M14 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 16l-4-4 4-4M6 12h10",
  file: "M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm7 0v5h5",
  chevron: "m9 6 6 6-6 6",
  arrowLeft: "M15 6l-6 6 6 6",
  upload: "M12 16V5m0 0-4 4m4-4 4 4M5 15v3a1 1 0 0 1 1 1h12a1 1 0 0 1 1-1v-3",
  send: "M5 12h13M13 6l6 6-6 6",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16, ...props }: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
