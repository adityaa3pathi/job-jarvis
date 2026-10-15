/** Jobs page filter state. It lives in the page URL, so every value is a plain string. */

export const STATUSES = ["found", "applied", "interview", "rejected", "dropped", "closed"];

export const SOURCE_LABELS: Record<string, string> = {
  linkedin: "LinkedIn",
  wellfound: "Wellfound",
  yc: "YC",
  cutshort: "Cutshort",
  hirist: "Hirist",
  jobgether: "Jobgether",
  bebee: "beBee",
  jobleads: "JobLeads",
  switchly: "Switchly",
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  workable: "Workable",
};

export const ROLE_LABELS: Record<string, string> = {
  ai_ml: "AI/ML",
  full_stack: "Full stack",
  backend: "Backend",
  frontend: "Frontend",
  mobile: "Mobile",
  software: "Software (general)",
  other: "Other",
};

export const WORK_MODE_LABELS: Record<string, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "On-site",
  unknown: "Not stated",
};

export const DATE_LABELS: Record<string, string> = {
  "1": "Last 24 hours",
  "3": "Last 3 days",
  "7": "Last 7 days",
  "30": "Last 30 days",
};

export const SORT_LABELS: Record<string, string> = {
  newest: "Newest first",
  best: "Best match",
  fewest_years: "Fewest years asked",
};

export const YEARS = ["1", "2", "3", "4"];
export const PAGE_SIZES = ["25", "50", "100"];
export const MAX_SEARCH = 60;

export type Filters = {
  status: string;
  source: string;
  role: string;
  work_mode: string;
  max_years: string;
  include_unstated: boolean;
  posted_within_days: string;
  city: string;
  company: string;
  q: string;
  sort: string;
  page: number;
  size: string;
};

export const DEFAULTS: Filters = {
  status: "found",
  source: "",
  role: "",
  work_mode: "",
  max_years: "",
  include_unstated: true,
  posted_within_days: "",
  city: "",
  company: "",
  q: "",
  sort: "newest",
  page: 1,
  size: "50",
};

const oneOf = (value: string | null, allowed: string[], fallback: string) =>
  value !== null && allowed.includes(value) ? value : fallback;

/** Unknown or hand-edited URL values fall back to defaults instead of making the API reply 422. */
export function readFilters(p: URLSearchParams): Filters {
  const page = Number.parseInt(p.get("page") ?? "", 10);
  return {
    status: oneOf(p.get("status"), STATUSES, DEFAULTS.status),
    source: (p.get("source") ?? "").toLowerCase().replace(/[^a-z0-9_]/g, "").slice(0, 40),
    role: oneOf(p.get("role"), Object.keys(ROLE_LABELS), ""),
    work_mode: oneOf(p.get("work_mode"), Object.keys(WORK_MODE_LABELS), ""),
    max_years: oneOf(p.get("max_years"), YEARS, ""),
    include_unstated: p.get("include_unstated") !== "false",
    posted_within_days: oneOf(p.get("posted_within_days"), Object.keys(DATE_LABELS), ""),
    city: (p.get("city") ?? "").trim().slice(0, 60),
    company: (p.get("company") ?? "").trim().slice(0, 200),
    q: (p.get("q") ?? "").slice(0, MAX_SEARCH),
    sort: oneOf(p.get("sort"), Object.keys(SORT_LABELS), DEFAULTS.sort),
    page: Number.isFinite(page) && page > 1 ? page : 1,
    size: oneOf(p.get("size"), PAGE_SIZES, DEFAULTS.size),
  };
}

/** URL query holding only the values that differ from the defaults. */
export function urlQuery(f: Filters): string {
  const p = new URLSearchParams();
  for (const key of Object.keys(DEFAULTS) as (keyof Filters)[]) {
    if (f[key] !== DEFAULTS[key]) p.set(key, String(f[key]));
  }
  return p.toString();
}

/** API query for one page of jobs; the API takes limit/offset rather than page/size. */
export function apiQuery(f: Filters): string {
  const p = new URLSearchParams({ status: f.status, sort: f.sort });
  for (const key of ["source", "role", "work_mode", "max_years", "posted_within_days", "city", "company", "q"] as const) {
    if (f[key]) p.set(key, f[key]);
  }
  if (!f.include_unstated) p.set("include_unstated", "false");
  const size = Number(f.size);
  p.set("limit", String(size));
  p.set("offset", String((f.page - 1) * size));
  return p.toString();
}

export const isFiltered = (f: Filters) =>
  urlQuery({ ...f, page: DEFAULTS.page, size: DEFAULTS.size }) !== "";

/** Page buttons with gaps, e.g. 1 … 4 5 [6] 7 8 … 40. */
export function pageList(current: number, last: number): (number | "gap")[] {
  const out: (number | "gap")[] = [];
  for (let n = 1; n <= last; n++) {
    if (n === 1 || n === last || Math.abs(n - current) <= 2) out.push(n);
    else if (out[out.length - 1] !== "gap") out.push("gap");
  }
  return out;
}

export function yearsTag(min: number | null, max: number | null): string {
  if (min === null && max === null) return "";
  if (min !== null && max !== null) return min === max ? `${min} yrs` : `${min}–${max} yrs`;
  return min !== null ? `${min}+ yrs` : `up to ${max} yrs`;
}
