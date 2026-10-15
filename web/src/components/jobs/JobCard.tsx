import Link from "next/link";
import { istDate, type Job } from "@/lib/api";
import { Button } from "@/components/ui";
import { ROLE_LABELS, SOURCE_LABELS, WORK_MODE_LABELS, yearsTag } from "./filters";

type Props = { job: Job; status: string; onMark: (job: Job, next: string) => void };

export function JobCard({ job: j, status, onMark }: Props) {
  const tags = [
    ROLE_LABELS[j.role] ?? "",
    WORK_MODE_LABELS[j.work_mode] ?? "",
    yearsTag(j.min_years, j.max_years),
  ].filter(Boolean);
  return (
    <li className="rounded-lg border border-zinc-200 bg-white p-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <a href={j.url ?? "#"} target="_blank" rel="noreferrer" className="font-medium hover:underline">
          {j.title}
        </a>
        <span className="text-zinc-600">· {j.company}</span>
        <span className="ml-auto text-xs text-zinc-500">
          {SOURCE_LABELS[j.source] ?? j.source} · {istDate(j.posted_date ?? j.seen_date)}
        </span>
      </div>
      <p className="text-sm text-zinc-600">
        {[j.location, j.experience_text, j.drop_reason].filter(Boolean).join(" · ")}
      </p>
      {tags.length > 0 && (
        <ul aria-label="Tags" className="mt-1 flex flex-wrap gap-1">
          {tags.map((t) => (
            <li key={t} className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs text-zinc-700">
              {t}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <Link
          className="rounded bg-zinc-900 px-3 py-1 text-sm text-white hover:bg-zinc-700"
          href={`/coach?url=${encodeURIComponent(j.url ?? "")}&title=${encodeURIComponent(j.title)}&company=${encodeURIComponent(j.company)}`}
        >
          Coach me
        </Link>
        {status !== "applied" && (
          <Button variant="ghost" onClick={() => onMark(j, "applied")}>
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
