/** Parchment-only decoration; hidden from screen readers and from the plain theme. */
export function Ornament({ kind = "divider" }: { kind?: "divider" | "quill" }) {
  if (kind === "quill") {
    return (
      <span aria-hidden="true" className="ornament ornament-quill">
        <svg viewBox="0 0 24 24" width="18" height="18" focusable="false">
          <path d="M21 3c-7 .5-12 5-14 12l-1.5 4.5 1.4-.6C8 16 9.5 14.5 11.5 14c5-1.3 8.7-5.6 9.5-11z" fill="currentColor" />
          <path d="M4 21 13 10" stroke="currentColor" strokeWidth="1.2" fill="none" />
        </svg>
      </span>
    );
  }
  return (
    <div aria-hidden="true" className="ornament ornament-divider">
      <svg viewBox="0 0 240 20" width="240" height="20" focusable="false">
        <path d="M14 10h82M144 10h82" stroke="currentColor" strokeWidth="1" />
        <path d="M96 10c6-8 14-8 24 0s18 8 24 0" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M96 10c6 8 14 8 24 0s18-8 24 0" fill="none" stroke="currentColor" strokeWidth="0.8" />
        <path d="M120 4l3.5 6-3.5 6-3.5-6z" fill="currentColor" />
        <circle cx="10" cy="10" r="2.5" fill="currentColor" />
        <circle cx="230" cy="10" r="2.5" fill="currentColor" />
      </svg>
    </div>
  );
}
