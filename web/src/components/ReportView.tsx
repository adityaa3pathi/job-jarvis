import type { Report } from "@/lib/api";
import { Chips, Panel } from "@/components/ui";

const fitTone = (fit: number) => (fit >= 4 ? "text-up" : fit >= 3 ? "text-ink" : "text-down");

export function ReportView({ report }: { report: Report }) {
  const warnings = [
    report.degraded_reason,
    report.flagged_injection && "This posting had text trying to steer the AI. It was ignored.",
    report.degraded && !report.degraded_reason && "The AI could not read the posting fully; skills come from a plain scan.",
  ].filter(Boolean) as string[];
  return (
    <div className="flex flex-col gap-4">
      <section className="flex flex-col gap-4 rounded-lg border border-line bg-panel p-5 sm:flex-row sm:items-start sm:gap-8">
        <div className="flex shrink-0 flex-col">
          <span className="text-[11px] font-bold uppercase tracking-wide text-ink-3">Fit</span>
          <span className={`figure text-6xl leading-none font-extrabold ${fitTone(report.fit)}`}>
            {report.fit}
            <span className="text-2xl text-ink-3">/5</span>
          </span>
          <span className="mt-1 text-xs text-ink-3">for {report.role_title || "this role"}</span>
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <p className="text-[15px] leading-relaxed">{report.summary}</p>
          {report.fit_reason && <p className="text-sm text-ink-2">Why: {report.fit_reason}</p>}
          {warnings.map((w) => (
            <p key={w} className="rounded-ctl bg-warn-soft px-3 py-2 text-sm text-warn">
              {w}
            </p>
          ))}
        </div>
      </section>
      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="What's good">
          {report.good.length > 0 && (
            <ul className="mb-3 flex flex-col gap-1.5 text-sm">
              {report.good.map((g) => (
                <li key={g} className="flex gap-2">
                  <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-up" />
                  {g}
                </li>
              ))}
            </ul>
          )}
          <Chips items={report.proven} tone="up" />
          {report.adjacent.length > 0 && (
            <>
              <h3 className="mt-4 mb-1.5 text-xs font-bold text-ink-3">Close, but not exact</h3>
              <Chips items={report.adjacent} tone="warn" />
            </>
          )}
        </Panel>
        <Panel title="What's missing">
          <h3 className="mb-1.5 text-xs font-bold text-ink-3">Must have</h3>
          <Chips items={report.missing_must} tone="down" />
          <h3 className="mt-4 mb-1.5 text-xs font-bold text-ink-3">Nice to have</h3>
          <Chips items={report.missing_nice} tone="plain" />
        </Panel>
      </div>
      <Panel title="What to learn">
        {report.learn.length === 0 ? (
          <p className="text-sm text-ink-3">Nothing urgent.</p>
        ) : (
          <ul className="-my-1 flex flex-col divide-y divide-line text-sm">
            {report.learn.map((l) => (
              <li key={l.topic} className="flex gap-4 py-3">
                <span className="figure w-14 shrink-0 text-right">
                  {l.days > 0 ? (
                    <>
                      <span className="text-lg font-extrabold">{l.days}</span>
                      <span className="text-xs text-ink-3"> days</span>
                    </>
                  ) : (
                    <span className="text-ink-3">–</span>
                  )}
                </span>
                <span className="min-w-0">
                  <span className="font-bold">{l.topic}</span>
                  <span className="mt-0.5 block text-ink-2">{l.why}</span>
                  {l.resource && <span className="mt-0.5 block text-ink-3">Start with: {l.resource}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      {report.risks.length > 0 && (
        <Panel title="Interview risks">
          <ul className="flex flex-col gap-1.5 text-sm">
            {report.risks.map((r) => (
              <li key={r} className="flex gap-2">
                <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-down" />
                {r}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}
