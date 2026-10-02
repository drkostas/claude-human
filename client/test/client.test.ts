import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createTaskClient,
  floorSteps,
  isFloor,
  TaskApiError,
  touchpointQuery,
  waitingLabel,
  type FetchLike,
  type PendingTask,
} from "../src";

type Call = { url: string; method: string; headers: Record<string, string>; body?: string };

/** A fake fetch that answers each call with the next reply, and records what was asked. */
function fakeFetch(...replies: Array<[number, unknown]>) {
  const calls: Call[] = [];
  const fn: FetchLike = async (url, init) => {
    calls.push({ url, ...init });
    const [status, body] = replies.length > 1 ? replies.shift()! : replies[0];
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  return { fn, calls };
}

const TASK: PendingTask = {
  intent: "7",
  subject: "app://backup",
  verb: "approve",
  tier: "confirm",
  reason: "The backup needs Full Disk Access.",
  steps: "Open System Settings and allow it.",
  waiting_s: 90,
  requires: "remote",
  handoffs: [
    { kind: "url", target: "https://example.org/x", label: null, preference: 10, platform: "any" },
    { kind: "steps", target: "Open System Settings and allow it.", label: "Follow the steps",
      preference: 9999, platform: "any" },
  ],
  head: { kind: "url", target: "https://example.org/x", label: null, preference: 10, platform: "any" },
};

afterEach(() => vi.unstubAllGlobals());

describe("createTaskClient", () => {
  it("needs a base URL", () => {
    expect(() => createTaskClient({ baseUrl: "" })).toThrow(/baseUrl/);
  });

  it("sends the bearer token and no Authorization header without one", async () => {
    const a = fakeFetch([200, { ok: true }]);
    await createTaskClient({ baseUrl: "http://127.0.0.1:1", token: "t0k", fetch: a.fn }).health();
    expect(a.calls[0].headers.Authorization).toBe("Bearer t0k");
    const b = fakeFetch([200, { ok: true }]);
    await createTaskClient({ baseUrl: "http://127.0.0.1:1", token: "  ", fetch: b.fn }).health();
    expect(b.calls[0].headers.Authorization).toBeUndefined();
  });

  it("reads a token function at each request", async () => {
    let token = "one";
    const f = fakeFetch([200, { ok: true }]);
    const c = createTaskClient({ baseUrl: "http://127.0.0.1:1", token: () => token, fetch: f.fn });
    await c.health();
    token = "two";
    await c.health();
    expect(f.calls.map((x) => x.headers.Authorization)).toEqual(["Bearer one", "Bearer two"]);
  });

  it("removes a trailing slash from the base URL", async () => {
    const f = fakeFetch([200, { ok: true }]);
    await createTaskClient({ baseUrl: "http://127.0.0.1:1///", fetch: f.fn }).health();
    expect(f.calls[0].url).toBe("http://127.0.0.1:1/health");
  });

  it("asks for pending with what the reader supports and where it runs", async () => {
    const f = fakeFetch([200, { pending: [TASK] }]);
    const c = createTaskClient({ baseUrl: "http://h", supports: ["steps", "url"], platform: "android",
                                fetch: f.fn });
    await expect(c.pending()).resolves.toEqual([TASK]);
    expect(f.calls[0].url).toBe("http://h/pending?supports=steps,url&platform=android");
    expect(f.calls[0].method).toBe("GET");
  });

  it("uses the server's defaults when the reader says nothing", () => {
    const c = createTaskClient({ baseUrl: "http://h", fetch: fakeFetch([200, {}]).fn });
    expect(c.touchpoint).toBe("supports=steps&platform=any");
  });

  it("reads one task and encodes its id", async () => {
    const f = fakeFetch([200, { task: TASK }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn });
    await expect(c.task("a/b c")).resolves.toEqual(TASK);
    expect(f.calls[0].url).toBe("http://h/task/a%2Fb%20c?supports=steps&platform=any");
  });

  it("turns a missing task into a TaskApiError with the status and body", async () => {
    const f = fakeFetch([404, { error: "not found" }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn, retries: 3, retryDelayMs: 0 });
    const err = await c.task("nope").catch((e) => e);
    expect(err).toBeInstanceOf(TaskApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe("HTTP 404");
    expect(err.body).toEqual({ error: "not found" });
    expect(f.calls).toHaveLength(1); // a 4xx is an answer, so it is not asked again
  });

  it("tries a failed GET again as often as asked, then throws", async () => {
    const f = fakeFetch([500, {}]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn, retries: 2, retryDelayMs: 0 });
    await expect(c.pending()).rejects.toThrow("HTTP 500");
    expect(f.calls).toHaveLength(3);
  });

  it("returns the second answer when the first try failed", async () => {
    const f = fakeFetch([503, {}], [200, { pending: [] }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn, retries: 1, retryDelayMs: 0 });
    await expect(c.pending()).resolves.toEqual([]);
  });

  it("never repeats a POST", async () => {
    const f = fakeFetch([500, {}]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn, retries: 5, retryDelayMs: 0 });
    await expect(c.done("7")).rejects.toBeInstanceOf(TaskApiError);
    expect(f.calls).toHaveLength(1);
  });

  it("done POSTs to /done/<id> with no body and returns the check's answer", async () => {
    const f = fakeFetch([200, { done: false, still_pending: true, detail: "Checked, and it is not done yet." }]);
    const c = createTaskClient({ baseUrl: "http://h", token: "x", fetch: f.fn });
    const r = await c.done("7");
    expect(r.done).toBe(false);
    expect(r.still_pending).toBe(true);
    expect(f.calls[0]).toMatchObject({ url: "http://h/done/7", method: "POST" });
    expect(f.calls[0].body).toBeUndefined();
  });

  it("open POSTs with the touchpoint so the server prepares the right head", async () => {
    const f = fakeFetch([200, { kind: "url", target: "https://example.org/x", prepared: null,
                               detail: "nothing to prepare" }]);
    const c = createTaskClient({ baseUrl: "http://h", supports: ["steps", "url"], platform: "ios",
                                fetch: f.fn });
    const r = await c.open("7");
    expect(r.prepared).toBeNull();
    expect(f.calls[0].url).toBe("http://h/open/7?supports=steps,url&platform=ios");
    expect(f.calls[0].method).toBe("POST");
  });

  it("comment sends the words as JSON and passes a refusal through", async () => {
    const f = fakeFetch([200, { ok: false, detail: "an empty comment says nothing" }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn });
    await expect(c.comment("7", "")).resolves.toEqual({ ok: false, detail: "an empty comment says nothing" });
    expect(f.calls[0].headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(f.calls[0].body!)).toEqual({ text: "" });
  });

  it("withdraw sends the reason, and an empty body without one", async () => {
    const f = fakeFetch([200, { ok: true, status: "done", outcome: "success", reason: "not needed" }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn });
    await c.withdraw("7", "not needed");
    await c.withdraw("7");
    expect(f.calls.map((x) => JSON.parse(x.body!))).toEqual([{ reason: "not needed" }, {}]);
    expect(f.calls[0].url).toBe("http://h/withdraw/7");
  });

  it("history and comments unwrap their lists", async () => {
    const f = fakeFetch([200, { history: [{ at: "t", verb: "task.done", subject: "s", outcome: "success" }] }],
                        [200, { comments: [{ id: 1, at: "t", intent: "7", subject: "s", said_by: null, text: "hi" }] }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn });
    expect((await c.history(10))[0].verb).toBe("task.done");
    expect((await c.comments())[0].text).toBe("hi");
    expect(f.calls[0].url).toBe("http://h/history?limit=10");
  });

  it("lets a caller type a server's extra fields", async () => {
    interface Mine extends PendingTask { risk: string }
    const f = fakeFetch([200, { pending: [{ ...TASK, risk: "low" }] }]);
    const c = createTaskClient({ baseUrl: "http://h", fetch: f.fn });
    const [t] = await c.pending<Mine>();
    expect(t.risk).toBe("low");
  });

  it("reads the global fetch when the call runs, so a stubbed global is used", async () => {
    const c = createTaskClient({ baseUrl: "http://h" });
    const fn = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }));
    vi.stubGlobal("fetch", fn);
    await c.health();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("getJson and postJson reach routes of the caller's own", async () => {
    const f = fakeFetch([200, { unseen: 3 }], [200, { ok: true }]);
    const c = createTaskClient({ baseUrl: "http://h", token: "x", fetch: f.fn });
    await expect(c.getJson<{ unseen: number }>("/unseen")).resolves.toEqual({ unseen: 3 });
    await c.postJson("/seen/abc", { a: 1 });
    expect(f.calls[1]).toMatchObject({ url: "http://h/seen/abc", method: "POST", body: '{"a":1}' });
    expect(c.headers()).toEqual({ Authorization: "Bearer x" });
  });
});

describe("helpers", () => {
  it("touchpointQuery encodes each kind and the platform", () => {
    expect(touchpointQuery(["steps", "a b"], "x/y")).toBe("supports=steps,a%20b&platform=x%2Fy");
  });

  it("waitingLabel formats seconds", () => {
    expect(waitingLabel(null)).toBe("");
    expect(waitingLabel(undefined)).toBe("");
    expect(waitingLabel(30)).toBe("just now");
    expect(waitingLabel(120)).toBe("waiting 2m");
    expect(waitingLabel(7200)).toBe("waiting 2h");
  });

  it("floorSteps finds the words of the chain, or the task's own", () => {
    expect(floorSteps(TASK)).toBe("Open System Settings and allow it.");
    expect(floorSteps({ handoffs: [], steps: "do it" })).toBe("do it");
    expect(floorSteps({ handoffs: [], steps: null })).toBeNull();
    expect(isFloor(TASK.handoffs[0])).toBe(false);
    expect(isFloor(TASK.handoffs[1])).toBe(true);
  });
});
