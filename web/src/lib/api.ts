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

/** Start a slow AI step, then poll until it finishes; AI calls outlast proxy timeouts. */
export async function runTask<T>(path: string, data?: unknown): Promise<T> {
  const { task_id } = await post<{ task_id: string }>(path, data);
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    const state = await api<TaskState<T>>(`/tasks/${task_id}`);
    if (state.status === "done") return state.result as T;
    if (state.status === "failed") throw new ApiError(state.code, state.error);
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
};

export type LearnItem = { topic: string; why: string; resource: string; days: number };

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

/** Dates show in IST as dd-mm-yyyy, the owner's format. */
export function istDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value.length === 10 ? `${value}T00:00:00+05:30` : value);
  return Number.isNaN(d.getTime()) ? value : IST.format(d).replaceAll("/", "-");
}
