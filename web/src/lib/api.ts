export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function detail(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e: { msg?: string }) => e.msg ?? "").join("; ");
  return fallback;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isForm = init.body instanceof FormData;
  const res = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: isForm ? init.headers : { "Content-Type": "application/json", ...init.headers },
  });
  if (res.status === 401 && path !== "/login") {
    window.location.href = "/login";
    throw new ApiError(401, "login needed");
  }
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) throw new ApiError(res.status, detail(body, `request failed (${res.status})`));
  return (body ?? text) as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const post = <T>(path: string, data?: unknown) =>
  api<T>(path, { method: "POST", body: data === undefined ? undefined : JSON.stringify(data) });

type TaskState<T> = { status: "running" | "done" | "failed"; result: T | null; error: string; code: number };

const POLL_MS = 1500;
const MAX_POLL_MISSES = 5;

/** Thrown when the caller stopped waiting (page left); callers should stay silent. */
export class TaskAborted extends Error {}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new TaskAborted("stopped"));
    const t = setTimeout(done, ms);
    function done() {
      signal?.removeEventListener("abort", stop);
      resolve();
    }
    function stop() {
      clearTimeout(t);
      reject(new TaskAborted("stopped"));
    }
    signal?.addEventListener("abort", stop, { once: true });
  });
}

/** Network errors and 5xx may clear up; 401, 404 and task failures will not. */
function transient(e: unknown): boolean {
  if (e instanceof ApiError) return e.status >= 500;
  return e instanceof TypeError;
}

/** Start a slow AI step, then poll the same task until it finishes; AI calls outlast proxy timeouts.
 * Failed polls are retried a few times, never by starting a second task. Abort `signal` on unmount. */
export async function runTask<T>(path: string, data?: unknown, signal?: AbortSignal): Promise<T> {
  const { task_id } = await post<{ task_id: string }>(path, data);
  let misses = 0;
  for (;;) {
    await wait(POLL_MS * (misses + 1), signal);
    let state: TaskState<T>;
    try {
      state = await api<TaskState<T>>(`/tasks/${task_id}`, { signal });
      misses = 0;
    } catch (e) {
      if (signal?.aborted) throw new TaskAborted("stopped");
      if (!transient(e)) throw e;
      if (++misses >= MAX_POLL_MISSES) {
        throw new ApiError(
          e instanceof ApiError ? e.status : 0,
          `Lost contact while waiting (${(e as Error).message}). It may still finish; reload before trying again.`,
        );
      }
      continue;
    }
    if (state.status === "done") return state.result as T;
    if (state.status === "failed") throw new ApiError(state.code, state.error || "the task failed");
  }
}

export type Job = {
  id: number;
  company: string;
  title: string;
  location: string;
  experience_text: string;
  posted_date: string | null;
  seen_date: string;
  source: string;
  status: string;
  drop_reason: string | null;
  score: number | null;
  url: string | null;
  min_years: number | null;
  max_years: number | null;
  role: string;
  work_mode: string;
  city: string;
};

/** One page of `GET /api/jobs`; `sources` counts ignore the source filter, zeros left out. */
export type JobPage = { items: Job[]; total: number; sources: Record<string, number> };

export type Facet = { name: string; count: number };

export type JobFacets = { companies: Facet[]; cities: Facet[] };

export type PortalCounts = { read: number; new: number; saved: number };

/** One discovery run as stored on the server; counts fill in as each portal finishes. */
export type DiscoverRun = {
  id: number;
  status: "running" | "done" | "failed";
  started_at: number;
  finished_at: number | null;
  error: string;
  read?: number;
  new?: number;
  saved?: number;
  portals?: Record<string, PortalCounts>;
  drop_reasons?: Record<string, number>;
  unreachable?: string[];
  skipped?: string[];
  notes?: string[];
};

/** `last` is `{}` before the first run ever finishes. */
export type DiscoverState = {
  started?: boolean;
  running: boolean;
  current: DiscoverRun | null;
  last: DiscoverRun | Record<string, never>;
};

export type LearnItem ={ topic: string; why: string; resource: string; days: number };

export type Report = {
  role_title: string;
  fit: number;
  summary: string;
  good: string[];
  proven: string[];
  adjacent: string[];
  missing_must: string[];
  missing_nice: string[];
  learn: LearnItem[];
  risks: string[];
  flagged_injection: boolean;
  degraded: boolean;
};

export type Build = { id: number; session_id: number; at: string; title: string; company: string };

export type Session = {
  id: number;
  title: string;
  company: string;
  source_url: string;
  created_at: string;
  report: Report;
  messages: { role: "user" | "coach"; text: string; at: string }[];
  facts: { id: number; text: string; confirmed: boolean }[];
  builds: Build[];
};

export type SessionRow = Pick<Session, "id" | "title" | "company" | "source_url" | "created_at">;

const IST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

const IST_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** Unix seconds as "dd-mm-yyyy HH:MM IST". */
export function istDateTime(seconds: number | null): string {
  if (!seconds) return "—";
  const d = new Date(seconds * 1000);
  return `${IST.format(d).replaceAll("/", "-")} ${IST_TIME.format(d)} IST`;
}

/** Dates show in IST as dd-mm-yyyy, the owner's format. */
export function istDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value.length === 10 ? `${value}T00:00:00+05:30` : value);
  return Number.isNaN(d.getTime()) ? value : IST.format(d).replaceAll("/", "-");
}
