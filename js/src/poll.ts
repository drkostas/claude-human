/** One read of the topic's backlog, announcing what is new since a watermark.
 *
 *  This has no Expo or React Native imports. background.ts runs it from an OS-scheduled task, and
 *  an app can run the same pass when it returns to the foreground. */
import {
  frameToNotification,
  newSince,
  parseBacklog,
  pollUrlFrom,
  ZERO_WATERMARK,
  type NotificationText,
  type NtfyFrame,
  type Watermark,
} from "./proto";

export interface PollOptions {
  /** the topic URL, for example https://ntfy.example.org/alerts */
  topicUrl: string;
  /** the stored position. A number is a time in seconds; null means "never watched". */
  readMark: () => Promise<Watermark | number | null>;
  /** store the new position. Called once per pass, after every message was announced. */
  commitMark?: (mark: Watermark) => Promise<void>;
  /** show one message, for example with raiseLocalNotification */
  announce: (n: { title: string; body: string; url?: string }, frame: NtfyFrame) => Promise<void>;
  /** how a frame reads as a notification */
  text?: NotificationText;
  /** backlog window, a Go duration or "all" (default "all") */
  since?: string;
  fetch?: typeof fetch;
}

/** Read the backlog once, announce the new messages oldest first, then commit the watermark.
 *  Returns how many were announced.
 *
 *  The mark is committed after the read, never before. Either order leaves a gap of a few
 *  milliseconds around the read. Committing after can miss a message published inside it, while
 *  committing before would show it twice, and the live socket covers the gap anyway. */
export async function pollTopicOnce(o: PollOptions): Promise<number> {
  const stored = await o.readMark();
  const mark: Watermark =
    stored == null ? ZERO_WATERMARK : typeof stored === "number" ? { time: stored, idsAtTime: [] } : stored;
  const get = o.fetch ?? fetch;
  const r = await get(pollUrlFrom(o.topicUrl, o.since));
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const frames = parseBacklog(await r.text());
  const next = newSince(frames, mark);
  for (const f of next.fresh.slice().reverse()) {
    await o.announce(frameToNotification(f, o.text), f);
  }
  await o.commitMark?.(next.mark);
  return next.fresh.length;
}
