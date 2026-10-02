/** A typed client for the task server (`claude-human task serve`).
 *
 *  Nothing is hardcoded. The caller passes the base URL and the bearer token, and says what this
 *  reader can show (`supports`) and where it runs (`platform`), so the server can order the chain
 *  for it. Every call reads the global `fetch` when it runs unless a `fetch` is passed in, so a test
 *  that stubs the global sees every request. */
import type {
  CommentItem,
  CommentResult,
  DoneResult,
  HealthResult,
  HistoryItem,
  OpenResult,
  PendingTask,
  WithdrawResult,
} from "./types";

/** The part of a fetch response the client reads. */
export interface FetchResponse {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}

/** Any function shaped like `fetch` for the calls the client makes. */
export type FetchLike = (
  url: string,
  init: { method: string; headers: Record<string, string>; body?: string },
) => Promise<FetchResponse>;

/** A token, or a function that answers the token at each request (so a new one takes effect
 *  without making a new client). An empty token sends no Authorization header. */
export type TokenSource = string | null | undefined | (() => string | null | undefined);

export interface TaskClientOptions {
  /** Where the server is, such as `http://127.0.0.1:8790`. A trailing slash is removed. */
  baseUrl: string;
  token?: TokenSource;
  /** The handoff kinds this reader can show. The server's default is `["steps"]`. */
  supports?: readonly string[];
  /** Where this reader runs (`android`, `ios`, `web`). The server's default is `any`. */
  platform?: string;
  fetch?: FetchLike;
  /** How many times a failed GET is tried again (default 0). POSTs are never repeated. */
  retries?: number;
  /** Milliseconds between tries of a GET (default 600). */
  retryDelayMs?: number;
  /** More headers for every request. */
  headers?: Record<string, string>;
}

/** A request the server answered with a status other than 2xx, or could not answer. The message
 *  is `HTTP <status>` so a caller can show it as it is. */
export class TaskApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body?: unknown) {
    super(`HTTP ${status}`);
    this.name = "TaskApiError";
    this.status = status;
    this.body = body;
  }
}

/** The query string that says what a reader can show and where it runs. */
export function touchpointQuery(supports: readonly string[], platform: string): string {
  return `supports=${supports.map(encodeURIComponent).join(",")}&platform=${encodeURIComponent(platform)}`;
}

function tokenOf(source: TokenSource): string {
  const t = typeof source === "function" ? source() : source;
  return (t ?? "").trim();
}

export interface TaskClient {
  readonly baseUrl: string;
  readonly supports: readonly string[];
  readonly platform: string;
  /** The `supports=...&platform=...` query this client sends. */
  readonly touchpoint: string;
  /** The headers each request carries (the bearer token and any extra headers). */
  headers(): Record<string, string>;
  /** GET a path on the server as JSON, with the retries from the options. */
  getJson<T>(path: string): Promise<T>;
  /** POST to a path on the server, with `body` as JSON when given. */
  postJson<T>(path: string, body?: unknown): Promise<T>;

  health(): Promise<HealthResult>;
  /** The open tasks, each with its chain ordered for this reader. */
  pending<T extends PendingTask = PendingTask>(): Promise<T[]>;
  /** One task by id. A task that does not exist is a `TaskApiError` with status 404. */
  task<T extends PendingTask = PendingTask>(id: string): Promise<T>;
  /** What happened, newest first. */
  history<T extends HistoryItem = HistoryItem>(limit?: number): Promise<T[]>;
  /** Every comment the store keeps. A server whose store keeps none answers 404. */
  comments(): Promise<CommentItem[]>;
  /** Ready the head of the chain for the person, before showing it. */
  open<T extends OpenResult = OpenResult>(id: string): Promise<T>;
  /** Ask the server to run the task's check. It never marks a task done by itself. */
  done<T extends DoneResult = DoneResult>(id: string): Promise<T>;
  /** Record what the person said about a task. */
  comment<T extends CommentResult = CommentResult>(id: string, text: string): Promise<T>;
  /** Drop an open task, with the person's reason. */
  withdraw<T extends WithdrawResult = WithdrawResult>(id: string, reason?: string): Promise<T>;
}

export function createTaskClient(options: TaskClientOptions): TaskClient {
  if (!options || typeof options.baseUrl !== "string" || !options.baseUrl) {
    throw new Error("createTaskClient needs a baseUrl");
  }
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const supports = [...(options.supports && options.supports.length ? options.supports : ["steps"])];
  const platform = options.platform || "any";
  const touchpoint = touchpointQuery(supports, platform);
  const retries = Math.max(0, options.retries ?? 0);
  const retryDelayMs = Math.max(0, options.retryDelayMs ?? 600);

  const doFetch: FetchLike = (url, init) => {
    if (options.fetch) return options.fetch(url, init);
    const f = (globalThis as { fetch?: unknown }).fetch as FetchLike | undefined;
    if (typeof f !== "function") throw new Error("no fetch: pass one in the options");
    return f(url, init);
  };

  const headers = (): Record<string, string> => {
    const out: Record<string, string> = { ...(options.headers ?? {}) };
    const token = tokenOf(options.token);
    if (token) out.Authorization = `Bearer ${token}`;
    return out;
  };

  async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
    const h = headers();
    const init: { method: string; headers: Record<string, string>; body?: string } = {
      method,
      headers: h,
    };
    if (method === "POST") h["Content-Type"] = "application/json";
    if (body !== undefined) init.body = JSON.stringify(body);
    const r = await doFetch(`${baseUrl}${path}`, init);
    if (!r.ok) {
      let detail: unknown;
      try {
        detail = await r.json();
      } catch {
        detail = undefined;
      }
      throw new TaskApiError(r.status, detail);
    }
    return (await r.json()) as T;
  }

  async function getJson<T>(path: string): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await send<T>("GET", path);
      } catch (e) {
        // a 4xx is an answer, and asking again gets the same one
        const answered = e instanceof TaskApiError && e.status >= 400 && e.status < 500;
        if (answered || attempt >= retries) throw e;
        await new Promise((res) => setTimeout(res, retryDelayMs));
      }
    }
  }

  const postJson = <T>(path: string, body?: unknown) => send<T>("POST", path, body);
  const id = (x: string) => encodeURIComponent(String(x));

  return {
    baseUrl,
    supports,
    platform,
    touchpoint,
    headers,
    getJson,
    postJson,
    health: () => getJson<HealthResult>("/health"),
    pending: async <T extends PendingTask = PendingTask>() =>
      (await getJson<{ pending: T[] }>(`/pending?${touchpoint}`)).pending,
    task: async <T extends PendingTask = PendingTask>(taskId: string) =>
      (await getJson<{ task: T }>(`/task/${id(taskId)}?${touchpoint}`)).task,
    history: async <T extends HistoryItem = HistoryItem>(limit = 50) =>
      (await getJson<{ history: T[] }>(`/history?limit=${Math.max(1, Math.floor(limit))}`)).history,
    comments: async () => (await getJson<{ comments: CommentItem[] }>("/comments")).comments,
    open: <T extends OpenResult = OpenResult>(taskId: string) =>
      postJson<T>(`/open/${id(taskId)}?${touchpoint}`),
    done: <T extends DoneResult = DoneResult>(taskId: string) => postJson<T>(`/done/${id(taskId)}`),
    comment: <T extends CommentResult = CommentResult>(taskId: string, text: string) =>
      postJson<T>(`/comment/${id(taskId)}`, { text }),
    withdraw: <T extends WithdrawResult = WithdrawResult>(taskId: string, reason?: string) =>
      postJson<T>(`/withdraw/${id(taskId)}`, reason ? { reason } : {}),
  };
}
