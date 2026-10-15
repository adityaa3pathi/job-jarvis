import { useEffect, useState, useSyncExternalStore } from "react";

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

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  const isForm = init.body instanceof FormData;
  const res = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: isForm ? init.headers : { "Content-Type": "application/json", ...init.headers },
  });
  if (res.status === 401 && path !== "/login") {
    clearCache();
    window.location.href = "/login";
    throw new ApiError(401, "login needed");
  }
  return res;
}

async function readBody<T>(res: Response): Promise<T> {
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) throw new ApiError(res.status, detail(body, `request failed (${res.status})`));
  return (body ?? text) as T;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return readBody<T>(await send(path, init));
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

/* Stale-while-revalidate cache for owner reads, keyed by API path. Shows the last copy
 * at once, then asks the server with If-None-Match; a 304 keeps the copy. */

type Entry = { data: unknown; etag: string | null };
type Slot = { data: unknown; refreshing: boolean; error: string | null };

const STORE_KEY = "jobagent:reads:v1";
const MAX_ENTRIES = 40;
const MAX_CHARS = 2_000_000; // well under the ~5 MB sessionStorage quota
const EMPTY: Slot = { data: undefined, refreshing: false, error: null };

const entries = new Map<string, Entry>(); // oldest use first, for LRU trimming
const slots = new Map<string, Slot>(); // what hooks render; stable objects for useSyncExternalStore
const inflight = new Map<string, Promise<unknown>>();
const gens = new Map<string, number>(); // bumped on invalidate so older replies are ignored
const mounted = new Map<string, number>();
const listeners = new Set<() => void>();
let restored = false;

function restore() {
  if (restored || typeof window === "undefined") return;
  restored = true;
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    for (const [k, e] of raw ? (JSON.parse(raw) as [string, Entry][]) : []) entries.set(k, e);
  } catch {
    // Blocked or corrupt storage: start empty.
  }
}

function persist() {
  try {
    let list = [...entries];
    let text = JSON.stringify(list);
    while (list.length > 0 && text.length > MAX_CHARS) {
      list = list.slice(1);
      text = JSON.stringify(list);
    }
    sessionStorage.setItem(STORE_KEY, text);
  } catch {
    // Quota or blocked storage: the memory copy still works.
  }
}

function remember(path: string, entry: Entry) {
  entries.delete(path);
  entries.set(path, entry);
  for (const k of entries.keys()) {
    if (entries.size <= MAX_ENTRIES) break;
    entries.delete(k);
  }
  persist();
}

function slot(path: string): Slot {
  restore();
  let s = slots.get(path);
  if (!s) {
    s = entries.has(path) ? { ...EMPTY, data: entries.get(path)!.data } : EMPTY;
    slots.set(path, s);
  }
  return s;
}

function setSlot(path: string, patch: Partial<Slot>) {
  slots.set(path, { ...slot(path), ...patch });
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** True when `path` is `prefix` itself or sits under it ("/coach" covers "/coach/3", not "/coach3"). */
function under(path: string, prefix: string): boolean {
  return path === prefix || path.startsWith(`${prefix}/`) || path.startsWith(`${prefix}?`);
}

/** Fetch `path` now (joining a request already on its way) and update the cache. */
export function revalidate<T>(path: string): Promise<T> {
  restore();
  const running = inflight.get(path);
  if (running) return running as Promise<T>;
  const gen = gens.get(path) ?? 0;
  const fresh = () => (gens.get(path) ?? 0) === gen;
  setSlot(path, { refreshing: true });
  const p = (async () => {
    const old = entries.get(path);
    try {
      const res = await send(path, {
        cache: "no-store", // the stored ETag below is the only revalidation
        headers: old?.etag ? { "If-None-Match": old.etag } : {},
      });
      let data: unknown;
      if (res.status === 304 && old) {
        data = old.data;
        if (fresh() && entries.has(path)) remember(path, old);
      } else {
        data = await readBody<unknown>(res);
        if (fresh()) remember(path, { data, etag: res.headers.get("ETag") });
      }
      if (fresh()) setSlot(path, { data, refreshing: false, error: null });
      return data as T;
    } catch (e) {
      if (fresh()) setSlot(path, { refreshing: false, error: (e as Error).message });
      throw e;
    }
  })();
  const settle = () => {
    if (inflight.get(path) === p) inflight.delete(path);
  };
  p.then(settle, settle);
  inflight.set(path, p);
  return p;
}

/** Change the shown copy of `path` right away (e.g. hide a marked job) until the next fetch. */
export function mutate<T>(path: string, change: (data: T) => T) {
  const s = slot(path);
  if (s.data !== undefined) setSlot(path, { data: change(s.data as T) });
}

/** Drop cached reads under `prefix` after a write; pages showing them refetch now. */
export function invalidate(prefix: string): Promise<unknown> {
  restore();
  for (const k of [...entries.keys()]) if (under(k, prefix)) entries.delete(k);
  persist();
  const refetch: Promise<unknown>[] = [];
  for (const k of new Set([...slots.keys(), ...inflight.keys()])) {
    if (!under(k, prefix)) continue;
    gens.set(k, (gens.get(k) ?? 0) + 1);
    inflight.delete(k);
    if (mounted.get(k)) refetch.push(revalidate(k));
  }
  return Promise.allSettled(refetch);
}

/** Forget every cached read, in memory and in sessionStorage (logout, 401, login). */
export function clearCache() {
  for (const k of new Set([...slots.keys(), ...inflight.keys(), ...entries.keys()])) {
    gens.set(k, (gens.get(k) ?? 0) + 1);
  }
  entries.clear();
  slots.clear();
  inflight.clear();
  try {
    sessionStorage.removeItem(STORE_KEY);
  } catch {
    // Blocked storage held nothing.
  }
  listeners.forEach((l) => l());
}

export type CachedRead<T> = {
  /** The newest copy for `path`, else the last one shown for an earlier path (see `key`). */
  data: T | null;
  key: string | null;
  loading: boolean;
  error: string | null;
  refreshing: boolean;
};

/** Read `path` through the cache: cached copy first, then a quiet refetch on every mount. */
export function useCachedApi<T>(path: string): CachedRead<T> {
  const s = useSyncExternalStore(
    subscribe,
    () => slot(path),
    () => EMPTY,
  );
  useEffect(() => {
    mounted.set(path, (mounted.get(path) ?? 0) + 1);
    revalidate(path).catch(() => {}); // the slot keeps the error
    return () => {
      mounted.set(path, (mounted.get(path) ?? 1) - 1);
    };
  }, [path]);
  const [last, setLast] = useState<{ key: string; data: T } | null>(null);
  if (s.data !== undefined && (last?.key !== path || last.data !== s.data)) {
    setLast({ key: path, data: s.data as T });
  }
  const shown = s.data !== undefined ? { key: path, data: s.data as T } : last;
  return {
    data: shown?.data ?? null,
    key: shown?.key ?? null,
    loading: s.data === undefined && s.error === null,
    error: s.error,
    refreshing: s.refreshing,
  };
}

type TaskState<T> ={ status: "running" | "done" | "failed"; result: T | null; error: string; code: number };

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
  score: number | null; // 0-100 "Best match" score
  rank_reason: string | null;
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
  fit_reason?: string; // older saved reports lack these two
  degraded_reason?: string;
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
