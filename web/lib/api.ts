export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
/** 日报 note 末尾 WORKLOG_TL_V1 哨兵载荷，全部为本机已脱敏文字。 */
export type WorklogPoint = { t: string; app: string; l: string };
export type WorklogAppTop = { app: string; seconds: number };
export type WorklogBrowserCategory = { category: string; visits: number };
export type WorklogStats = {
  active_human: number | string;
  git_commits: number;
  ai_sessions: number;
  ai_messages: number;
  files_changed: number;
  app_top: WorklogAppTop[];
  browser_categories: WorklogBrowserCategory[];
};
export type WorklogTL = {
  v: number;
  day: string;
  pts: WorklogPoint[];
  hl: string;
  stats: WorklogStats;
};
/** 类型守卫：把 JSON.parse 的 unknown 收窄为 WORKLOG_TL_V1 载荷。 */
export function isWorklogTL(value: unknown): value is WorklogTL {
  if (!value || typeof value !== "object") return false;
  const w = value as Record<string, unknown>;
  const s = w.stats;
  return (
    w.v === 1 &&
    typeof w.day === "string" &&
    Array.isArray(w.pts) &&
    typeof w.hl === "string" &&
    !!s &&
    typeof s === "object" &&
    Array.isArray((s as Record<string, unknown>).app_top) &&
    Array.isArray((s as Record<string, unknown>).browser_categories)
  );
}
export type Content = {
  id: string;
  type: "blog" | "document" | "manual" | "automatic";
  title: string;
  summary: string;
  markdown: string;
  date: string;
  visibility: "public" | "private";
  rag_enabled: boolean;
  category: string;
  tags: string[];
  revision: number;
  published?: boolean;
  published_version_id?: string;
  /** 可选：前端从 markdown 的 WORKLOG_TL_V1 哨兵解析后回填，API 不直接返回。 */
  worklog?: WorklogTL | null;
  [key: string]: unknown;
};
export type NavigationItem = {
  id: string;
  label: string;
  href: string;
  event_name?: string;
  visible?: boolean;
  show_as_bot?: boolean;
  bot_size?: number;
  sort_order?: number;
};
export type Site = {
  profile?: {
    name?: string;
    bio?: string;
    introduction?: string;
    hero?: string;
  };
  projects?: Record<string, string>[];
  stack?: Record<string, string>[];
  interests?: Record<string, string>[];
  seo?: { title?: string; description?: string };
  navigation?: NavigationItem[];
  [key: string]: unknown;
};
let csrf = "";
export function setCsrf(token: string) {
  csrf = token;
}
export async function api<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method)) {
    if (!csrf) {
      const session = await fetch("/api/v1/auth/session", {
        credentials: "include",
      }).then((r) => r.json());
      csrf = session.csrf_token || "";
    }
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    if (response.status === 401 && path.startsWith("/admin"))
      window.dispatchEvent(new Event("lyph:auth-expired"));
    const body = await response.json().catch(() => ({}));
    const detail = body.detail || body.error || `请求失败 (${response.status})`;
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : JSON.stringify(detail),
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export async function serverApi<T>(path: string): Promise<T> {
  const response = await fetch(
    `${process.env.API_BASE_URL || "http://127.0.0.1:8000"}/api/v1${path}`,
    { cache: "no-store", signal: AbortSignal.timeout(8000) },
  );
  if (!response.ok)
    throw new ApiError(response.status, `内容读取失败 (${response.status})`);
  return response.json();
}
export function items<T>(data: unknown): T[] {
  if (Array.isArray(data)) return data;
  if (data && typeof data === "object") {
    const d = data as Record<string, unknown>;
    return (d.items || d.contents || d.results || d.data || []) as T[];
  }
  return [];
}
export const json = (value: unknown) => JSON.stringify(value);
export const errorText = (e: unknown) =>
  e instanceof Error ? e.message : "请求未完成，请稍后重试。";
export const typeNames = {
  blog: "博客",
  document: "文档",
  manual: "手写日记",
  automatic: "自动日报",
};
