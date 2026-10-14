import { describe, expect, it } from "vitest";
import { pollTopicOnce, type Watermark } from "../src/index";

function backlog(...frames: object[]) {
  const body = frames.map((f) => JSON.stringify(f)).join("\n");
  const urls: string[] = [];
  const fetch = (async (url: string) => {
    urls.push(url);
    return { ok: true, status: 200, text: async () => body } as Response;
  }) as unknown as typeof globalThis.fetch;
  return { fetch, urls };
}

const msg = (id: string, time: number, title = id) => ({ id, time, event: "message", title, message: "m" + id });

describe("pollTopicOnce", () => {
  it("announces new messages oldest first, then commits the mark", async () => {
    const { fetch, urls } = backlog(msg("a", 10), msg("b", 20), msg("c", 30));
    const shown: string[] = [];
    const order: string[] = [];
    let committed: Watermark | null = null;
    const n = await pollTopicOnce({
      topicUrl: "https://ntfy.example.org/t",
      readMark: async () => 10.5, // a stored time in seconds, as a float
      announce: async (note) => {
        shown.push(note.title);
        order.push("announce");
      },
      commitMark: async (m) => {
        committed = m;
        order.push("commit");
      },
      fetch,
    });
    expect(n).toBe(2);
    expect(shown).toEqual(["b", "c"]);
    expect(order).toEqual(["announce", "announce", "commit"]);
    expect(committed).toEqual({ time: 30, idsAtTime: ["c"] });
    expect(urls).toEqual(["https://ntfy.example.org/t/json?poll=1&since=all"]);
  });

  it("announces nothing on a device that never watched", async () => {
    const { fetch } = backlog(msg("a", 10), msg("b", 20));
    const shown: string[] = [];
    const n = await pollTopicOnce({
      topicUrl: "https://h/t",
      readMark: async () => null,
      announce: async (note) => void shown.push(note.title),
      fetch,
    });
    expect(n).toBe(0);
    expect(shown).toEqual([]);
  });

  it("passes the notification text options through", async () => {
    const { fetch } = backlog({ id: "x", time: 5, event: "message", click: "myapp://a" });
    const notes: object[] = [];
    await pollTopicOnce({
      topicUrl: "https://h/t",
      readMark: async () => ({ time: 1, idsAtTime: [] }),
      announce: async (note) => void notes.push(note),
      text: { appName: "Lab", emptyBody: "news", linkPrefix: "myapp://" },
      fetch,
    });
    expect(notes).toEqual([{ title: "Lab", body: "news", url: "myapp://a" }]);
  });

  it("throws on an HTTP error and commits nothing", async () => {
    let committed = false;
    const fetch = (async () => ({ ok: false, status: 400, text: async () => "" })) as unknown as typeof globalThis.fetch;
    await expect(
      pollTopicOnce({
        topicUrl: "https://h/t",
        readMark: async () => 1,
        announce: async () => undefined,
        commitMark: async () => void (committed = true),
        fetch,
      }),
    ).rejects.toThrow("HTTP 400");
    expect(committed).toBe(false);
  });
});


describe("pollTopicOnce's time limit", () => {
  it("gives up on a topic that never answers, announces nothing and keeps the mark", async () => {
    let signal: AbortSignal | undefined;
    const silent = ((_url: string, init?: RequestInit) => {
      signal = init?.signal ?? undefined;
      return new Promise<Response>(() => {});            // never answers, ignores the signal
    }) as unknown as typeof globalThis.fetch;
    let announced = 0;
    let committed = false;
    const started = Date.now();
    await expect(pollTopicOnce({
      topicUrl: "https://ntfy.example.org/t",
      readMark: async () => null,
      announce: async () => { announced += 1; },
      commitMark: async () => { committed = true; },
      fetch: silent,
      timeoutMs: 50,
    })).rejects.toThrow("did not answer within 50 ms");
    expect(Date.now() - started).toBeLessThan(2000);
    expect(signal?.aborted).toBe(true);
    expect(announced).toBe(0);
    expect(committed).toBe(false);
  });

  it("does not cut a read that answers in time", async () => {
    const { fetch } = backlog(msg("a", 10));
    const n = await pollTopicOnce({ topicUrl: "https://ntfy.example.org/t", readMark: async () => 5,
                                    announce: async () => {}, fetch, timeoutMs: 1000 });
    expect(n).toBe(1);
  });
});
