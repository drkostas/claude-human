import { describe, expect, it } from "vitest";
import {
  assertReachableFromPhone,
  frameToNotification,
  isLoopbackHost,
  newSince,
  parseBacklog,
  parseFrame,
  pollUrlFrom,
  wsUrlFrom,
  ZERO_WATERMARK,
  type NtfyFrame,
} from "../src/index";

describe("urls", () => {
  it("derives the WebSocket URL from the topic URL", () => {
    expect(wsUrlFrom("https://ntfy.example.org:8443/alerts")).toBe("wss://ntfy.example.org:8443/alerts/ws");
    expect(wsUrlFrom("http://10.0.0.5:8080/alerts/")).toBe("ws://10.0.0.5:8080/alerts/ws");
  });

  it("asks for the backlog with a duration Go can parse", () => {
    expect(pollUrlFrom("https://h/t", "6h")).toBe("https://h/t/json?poll=1&since=6h");
    expect(pollUrlFrom("https://h/t/")).toBe("https://h/t/json?poll=1&since=all");
  });
});

describe("frames", () => {
  it("keeps message frames and drops open, keepalive and garbage", () => {
    expect(parseFrame(JSON.stringify({ id: "a", event: "message", message: "hi" }))?.message).toBe("hi");
    expect(parseFrame(JSON.stringify({ id: "b", event: "open" }))).toBeNull();
    expect(parseFrame(JSON.stringify({ id: "c", event: "keepalive" }))).toBeNull();
    expect(parseFrame("not json")).toBeNull();
    expect(parseFrame("null")).toBeNull();
  });

  it("parses a backlog newest first and survives junk lines", () => {
    const body = [
      JSON.stringify({ id: "1", event: "open" }),
      JSON.stringify({ id: "2", event: "message", message: "older" }),
      "",
      "not json",
      JSON.stringify({ id: "3", event: "message", message: "newer" }),
    ].join("\n");
    expect(parseBacklog(body).map((f) => f.message)).toEqual(["newer", "older"]);
  });

  it("fills a title and body when the frame has none", () => {
    expect(frameToNotification({ id: "1", event: "message", title: "Disk", message: "almost full" }))
      .toEqual({ title: "Disk", body: "almost full" });
    expect(frameToNotification({ id: "2", event: "message" })).toEqual({ title: "Notification", body: "Notification" });
    expect(frameToNotification({ id: "2", event: "message" }, { appName: "Lab", emptyBody: "Something happened" }))
      .toEqual({ title: "Lab", body: "Something happened" });
    expect(frameToNotification({ id: "3", event: "message", title: "Only title" }))
      .toEqual({ title: "Only title", body: "Only title" });
  });

  it("keeps a click URL only when it points into the app", () => {
    const f = { id: "1", event: "message", click: "myapp://task/42" };
    expect(frameToNotification(f).url).toBeUndefined();
    expect(frameToNotification(f, { linkPrefix: "myapp://" }).url).toBe("myapp://task/42");
    expect(frameToNotification({ ...f, click: "https://evil.example/x" }, { linkPrefix: "myapp://" }).url)
      .toBeUndefined();
  });
});

describe("watermark", () => {
  const f = (id: string, time: number): NtfyFrame => ({ id, event: "message", time, message: id });

  it("adopts the position on first read instead of replaying history", () => {
    const frames = [f("c", 30), f("b", 20), f("a", 10)];
    const first = newSince(frames, ZERO_WATERMARK);
    expect(first.fresh).toEqual([]);
    expect(first.mark).toEqual({ time: 30, idsAtTime: ["c"] });
    expect(newSince([f("d", 40), ...frames], first.mark).fresh.map((x) => x.id)).toEqual(["d"]);
  });

  it("announces only what arrived after the mark", () => {
    const seen = newSince([f("b", 20), f("a", 10)], ZERO_WATERMARK);
    expect(newSince([f("c", 30), f("b", 20), f("a", 10)], seen.mark).fresh.map((x) => x.id)).toEqual(["c"]);
  });

  it("neither skips nor repeats messages that share a second", () => {
    const seen = newSince([f("a", 20)], ZERO_WATERMARK);
    const next = newSince([f("b", 20), f("a", 20)], seen.mark);
    expect(next.fresh.map((x) => x.id)).toEqual(["b"]);
    expect(newSince([f("b", 20), f("a", 20)], next.mark).fresh).toEqual([]);
  });

  it("leaves the mark alone after an empty read", () => {
    const seen = newSince([f("a", 10)], ZERO_WATERMARK);
    expect(newSince([], seen.mark).mark).toEqual(seen.mark);
  });
});

describe("loopback guard", () => {
  it("refuses addresses a phone cannot reach", () => {
    for (const h of ["localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]", "0.0.0.0", "", "app.localhost"]) {
      expect(isLoopbackHost(h)).toBe(true);
    }
    for (const h of ["ntfy.example.org", "10.0.0.5", "192.168.1.20", "host.tail.example.net"]) {
      expect(isLoopbackHost(h)).toBe(false);
    }
    expect(() => assertReachableFromPhone("http://localhost:8080/t")).toThrow(/cannot be reached/);
    expect(() => assertReachableFromPhone(undefined)).toThrow();
    expect(assertReachableFromPhone("https://ntfy.example.org/t")).toBe("https://ntfy.example.org/t");
  });
});
