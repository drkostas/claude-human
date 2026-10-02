/** The wire logic of subscribing to a self-hosted ntfy topic.
 *
 *  Nothing here imports React, Expo or React Native, so it runs and is tested in plain Node. The
 *  device glue (permissions, local notifications, the socket lifecycle) is in notify.ts. */

/** One frame from ntfy's WebSocket or JSON stream. ntfy sends open, keepalive and message frames.
 *  Only message frames carry something a person should see. */
export interface NtfyFrame {
  id: string;
  time?: number;
  event: "open" | "keepalive" | "message" | "poll_request" | string;
  topic?: string;
  message?: string;
  title?: string;
  priority?: number;
  tags?: string[];
  /** ntfy's Click: where the message is about, often a deep link into the app */
  click?: string;
  /** ntfy's Attach (a URL) or an uploaded attachment */
  attach?: string;
  /** file name of the attachment, when ntfy sent one */
  filename?: string;
  /** ntfy's Actions: view (open a URL), http (call an endpoint), broadcast */
  actions?: NtfyAction[];
}

export interface NtfyAction {
  action: "view" | "http" | "broadcast" | string;
  label: string;
  url?: string;
  method?: string;
  body?: string;
  headers?: Record<string, string>;
  clear?: boolean;
}

/** The topic's backlog URL. `since` is parsed as a Go duration, which has no day unit: "7d" makes
 *  the server answer 400, while "24h", "all" or a Unix time work. */
export function pollUrlFrom(topicUrl: string, since = "all"): string {
  return `${topicUrl.replace(/\/+$/, "")}/json?poll=1&since=${since}`;
}

/** https://host/topic -> wss://host/topic/ws (and http -> ws). */
export function wsUrlFrom(topicUrl: string): string {
  const base = topicUrl.replace(/\/+$/, "");
  return base.replace(/^http(s?):/, "ws$1:") + "/ws";
}

/** One raw frame, or null. Only `message` frames are returned (never open or keepalive), and
 *  invalid JSON gives null, so one bad frame cannot stop a socket loop. */
export function parseFrame(raw: string): NtfyFrame | null {
  let f: NtfyFrame;
  try {
    f = JSON.parse(raw) as NtfyFrame;
  } catch {
    return null;
  }
  if (!f || f.event !== "message") return null;
  return f;
}

/** ndjson (one frame per line) -> the message frames, newest first. */
export function parseBacklog(body: string): NtfyFrame[] {
  return body
    .split("\n")
    .map((l) => parseFrame(l))
    .filter((f): f is NtfyFrame => f !== null)
    .reverse();
}

export interface NotificationText {
  /** title used when the frame has none (default "Notification") */
  appName?: string;
  /** body used when the frame has neither a message nor a title */
  emptyBody?: string;
  /** a click URL is kept only when it starts with this prefix (for example "myapp://").
   *  Without a prefix no click URL is kept. A notification is data from the network, and a link
   *  in it should not be followed unless it points into the app. */
  linkPrefix?: string;
}

/** How a message frame should read in the notification tray. */
export function frameToNotification(
  f: NtfyFrame,
  opts: NotificationText = {},
): { title: string; body: string; url?: string } {
  const appName = opts.appName ?? "Notification";
  const title = (f.title && f.title.trim()) || appName;
  const body = (f.message && f.message.trim()) || f.title || opts.emptyBody || appName;
  const url = opts.linkPrefix && f.click && f.click.startsWith(opts.linkPrefix) ? f.click : undefined;
  return url ? { title, body, url } : { title, body };
}

/** How far through the topic a device has already announced.
 *
 *  A timestamp alone is not enough, because ntfy stamps whole seconds and several messages can
 *  share one. The ids seen at that second are kept too, so none is skipped or shown twice. */
export interface Watermark {
  time: number;
  idsAtTime: string[];
}

export const ZERO_WATERMARK: Watermark = { time: 0, idsAtTime: [] };

/** The frames not announced yet, and the watermark after them.
 *
 *  On a zero watermark nothing is announced and the position is adopted instead. A device that
 *  never watched has not missed anything, and announcing the topic's whole history on first launch
 *  would bury the person in old messages. */
export function newSince(
  frames: NtfyFrame[],
  mark: Watermark,
): { fresh: NtfyFrame[]; mark: Watermark } {
  const at = (f: NtfyFrame) => f.time ?? 0;
  if (mark.time === 0 && mark.idsAtTime.length === 0) {
    const maxTime = frames.reduce((m, f) => Math.max(m, at(f)), 0);
    return {
      fresh: [],
      mark: { time: maxTime, idsAtTime: frames.filter((f) => at(f) === maxTime).map((f) => f.id) },
    };
  }
  const fresh = frames.filter(
    (f) => at(f) > mark.time || (at(f) === mark.time && !mark.idsAtTime.includes(f.id)),
  );
  const maxTime = frames.reduce((m, f) => Math.max(m, at(f)), mark.time);
  const idsAtMax = frames.filter((f) => at(f) === maxTime).map((f) => f.id);
  return {
    fresh,
    mark: {
      time: maxTime,
      idsAtTime: maxTime === mark.time ? [...new Set([...mark.idsAtTime, ...idsAtMax])] : idsAtMax,
    },
  };
}

/** Throws when a topic URL is one a phone cannot reach (empty, invalid, or loopback). A phone
 *  that is given "localhost" connects to itself and never receives anything. */
export function assertReachableFromPhone(url: string | undefined): string {
  let host = "";
  try {
    host = new URL(url ?? "").hostname;
  } catch {
    host = "";
  }
  if (isLoopbackHost(host)) {
    throw new Error(
      `ntfy topic URL ${JSON.stringify(url ?? "")} cannot be reached from a phone. ` +
        "Use an address of the server that the phone can reach (not localhost).",
    );
  }
  return url as string;
}

export function isLoopbackHost(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, "").toLowerCase();
  return h === "" || h === "localhost" || h.endsWith(".localhost") || h === "::1" || h === "0.0.0.0"
    || /^127\.\d+\.\d+\.\d+$/.test(h);
}
