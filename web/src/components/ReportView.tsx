import type { Report } from "@/lib/api";
import { Card, Chips } from "@/components/ui";

export function ReportView({ report }: { report: Report }) {
  return (
    <div className="flex flex-col gap-3">
      <Card>
        <div className="flex items-baseline gap-3">
          <span className="text-3xl font-semibold">{report.fit}/5</span>
          <span className="text-zinc-600">fit for {report.role_title || "this role"}</span>
        </div>
        <p className="mt-2">{report.summary}</p>
        {report.flagged_injection && (
          <p className="mt-2 text-sm text-amber-800">This posting had text trying to steer the AI. It was ignored.</p>
        )}
        {report.degraded && (
          <p className="mt-2 text-sm text-amber-800">The AI could not read the posting fully; skills come from a plain scan.</p>
        )}
      </Card>
      <div className="grid gap-3 md:grid-cols-2">
        <Card title="What's good">
          <ul className="mb-2 list-disc pl-5 text-sm">
            {report.good.map((g) => (
              <li key={g}>{g}</li>
            ))}
          </ul>
          <Chips items={report.proven} tone="good" />
          {report.adjacent.length > 0 && (
            <>
              <p className="mt-2 text-sm text-zinc-600">Close, but not exact:</p>
              <Chips items={report.adjacent} tone="warn" />
            </>
          )}
        </Card>
        <Card title="What's missing">
          <p className="text-sm text-zinc-600">Must have:</p>
          <Chips items={report.missing_must} tone="bad" />
          <p className="mt-2 text-sm text-zinc-600">Nice to have:</p>
          <Chips items={report.missing_nice} tone="plain" />
        </Card>
      </div>
      <Card title="What to learn">
        {report.learn.length === 0 ? (
          <p className="text-sm text-zinc-500">Nothing urgent.</p>
        ) : (
          <ul className="flex flex-col gap-2 text-sm">
            {report.learn.map((l) => (
              <li key={l.topic}>
                <span className="font-medium">{l.topic}</span>
                {l.days > 0 && <span className="text-zinc-500"> · about {l.days} days</span>}
                <p className="text-zinc-700">{l.why}</p>
                {l.resource && <p className="text-zinc-500">Start with: {l.resource}</p>}
              </li>
            ))}
          </ul>
        )}
      </Card>
      {report.risks.length > 0 && (
        <Card title="Interview risks">
          <ul className="list-disc pl-5 text-sm">
            {report.risks.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
