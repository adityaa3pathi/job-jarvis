import Link from "next/link";
import { istDate, type Job } from "@/lib/api";
import { Button, PRIMARY } from "@/components/ui";
import { Seal, type Tone } from "@/components/Seal";
import { ROLE_LABELS, SOURCE_LABELS, WORK_MODE_LABELS, yearsTag } from "./filters";

const scoreTone = (score: number): Tone => (score >= 70 ? "good" : score >= 40 ? "plain" : "warn");

type Props = { job: Job; status: string; onMark: (job: Job, next: string) => void };

export function JobCard({ job: j, status, onMark }: Props) {
  const tags = [
    ROLE_LABELS[j.role] ?? "",
    WORK_MODE_LABELS[j.work_mode] ?? "",
    yearsTag(j.min_years, j.max_years),
  ].filter(Boolean);
  return (
    <li className="card rounded-lg border border-border bg-surface-raised p-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <a href={j.url ?? "#"} target="_blank" rel="noreferrer" className="font-medium hover:underline">
          {j.title}
        </a>
        <span className="text-ink-muted">· {j.company}</span>
        {j.score != null && (
          <Seal tone={scoreTone(j.score)} className="px-1.5 py-0.5 text-xs font-semibold tabular-nums">
            <span title="Match score out of 100">{j.score}</span>
          </Seal>
        )}
        <span className="ml-auto text-xs text-ink-faint">
          {SOURCE_LABELS[j.source] ?? j.source} · {istDate(j.posted_date ?? j.seen_date)}
        </span>
      </div>
      <p className="text-sm text-ink-muted">
        {[j.location, j.experience_text, j.drop_reason].filter(Boolean).join(" · ")}
      </p>
      {j.rank_reason && (
        <p className="truncate text-xs text-ink-faint" title={j.rank_reason}>
          {j.rank_reason}
        </p>
      )}
      {tags.length > 0 && (
        <ul aria-label="Tags" className="mt-1 flex flex-wrap gap-1">
          {tags.map((t) => (
            <li key={t} className="rounded bg-surface-muted px-1.5 py-0.5 text-xs text-ink-muted">
              {t}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <Link
          className={`rounded px-3 py-1 text-sm ${PRIMARY}`}
          href={`/coach?url=${encodeURIComponent(j.url ?? "")}&title=${encodeURIComponent(j.title)}&company=${encodeURIComponent(j.company)}`}
        >
          Coach me
        </Link>
        {status !== "applied" && (
          <Button variant="ghost" className="stamp" onClick={() => onMark(j, "applied")}>
            Applied
          </Button>
        )}
        {status !== "dropped" && (
          <Button variant="ghost" onClick={() => onMark(j, "dropped")}>
            Skip
          </Button>
        )}
        {status !== "found" && (
          <Button variant="ghost" onClick={() => onMark(j, "found")}>
            Back to list
          </Button>
        )}
      </div>
    </li>
  );
}
