/** The pure part of the example: routes, what the task page shows, and the words for each answer.
 *  No React and no React Native here, so it runs under vitest in Node. */
import {
  floorSteps,
  isFloor,
  waitingLabel,
  type DoneResult,
  type Handoff,
  type HistoryItem,
  type OpenResult,
  type PendingTask,
} from "@drkostas/claude-human-client";

export type Route = { name: "pending" } | { name: "task"; id: string } | { name: "history" };

/** The screen a link opens, or null when the link is not this app's. A notification is data from
 *  the network, so only links that start with the app's own prefix are followed. */
export function routeFor(url: string | undefined | null, prefix: string): Route | null {
  if (!url || !prefix || !url.startsWith(prefix)) return null;
  const rest = url.slice(prefix.length).replace(/^\/+/, "").replace(/[?#].*$/, "");
  if (rest === "" || rest === "pending") return { name: "pending" };
  if (rest === "history") return { name: "history" };
  const m = /^task\/([^/]+)\/?$/.exec(rest);
  if (m) {
    try {
      return { name: "task", id: decodeURIComponent(m[1]) };
    } catch {
      return null;
    }
  }
  return null;
}

/** The kinds this app can open itself. It tells the server, which then sends only these. */
export const SUPPORTS = ["steps", "url", "settings"] as const;

export function canOpen(h: Pick<Handoff, "kind" | "target">): boolean {
  return (h.kind === "url" || h.kind === "settings") && !!h.target;
}

/** One row of the handoff chain on the task page. */
export interface ChainRow {
  key: string;
  title: string;
  detail: string;
  /** the first surface, chosen by the server */
  isHead: boolean;
  /** the steps, which are always there */
  isFloor: boolean;
  openable: boolean;
}

const KIND_TITLES: Record<string, string> = {
  url: "Open the page",
  settings: "Open the settings",
  vnc: "Open the window",
  shell: "Run the command",
  steps: "Follow the steps",
};

/** The chain in the server's order. The app does not sort it again. The head is marked, and the
 *  floor is the last row with a note that it always works. */
export function chainRows(task: Pick<PendingTask, "handoffs" | "head">): ChainRow[] {
  return (task.handoffs ?? []).map((h, i) => {
    const floor = isFloor(h);
    const head = !!task.head && !floor && h.kind === task.head.kind && h.target === task.head.target;
    return {
      key: `${i}:${h.kind}`,
      title: h.label || KIND_TITLES[h.kind] || h.kind,
      detail: floor ? "Always works, even with nothing to open." : h.target ?? "",
      isHead: head,
      isFloor: floor,
      openable: !floor && canOpen(h),
    };
  });
}

/** The two parts every task page starts with: why the person is asked, and what to do. */
export function taskSections(task: Pick<PendingTask, "reason" | "steps" | "handoffs" | "verb" | "subject">) {
  return {
    title: `${task.verb}: ${task.subject}`,
    whyYou: task.reason || "No reason was given.",
    whatToDo: floorSteps(task) || "There are no written steps. Use the first way to reach it below.",
  };
}

/** The line under a task in the pending list. */
export function pendingLine(task: Pick<PendingTask, "subject" | "waiting_s">): string {
  const w = waitingLabel(task.waiting_s);
  return w ? `${task.subject}, ${w}` : task.subject;
}

/** What to tell the person after "I've done it, check". The check decides, never the button. */
export function doneMessage(r: DoneResult): { tone: "done" | "waiting"; text: string } {
  if (r.done) return { tone: "done", text: r.detail || "Checked, and it is done." };
  return { tone: "waiting", text: r.detail || "Checked, and it is not done yet." };
}

/** What to tell the person after asking the server to prepare a surface, or null when it is ready
 *  to show. A surface that could not be prepared is never shown as ready. */
export function openProblem(r: OpenResult): string | null {
  if (r.prepared === false) return `It is not ready: ${r.detail}`;
  if (!r.target) return "There is nothing to open for this task. Follow the steps.";
  return null;
}

const EVENT_TITLES: Record<string, string> = {
  "task.open": "Asked",
  "task.done": "Done",
  "task.withdraw": "Dropped",
  "task.comment": "Comment",
  "task.notify": "Told the person",
  "task.refused": "Refused",
  "task.unknown": "Checked, no answer",
};

/** One line of history as the person reads it. */
export function historyLine(item: HistoryItem): { title: string; subject: string; says: string | null } {
  const title = EVENT_TITLES[item.verb] ?? item.verb;
  const subject = [item.capability, item.subject].filter(Boolean).join(" ") || "(no subject)";
  return { title, subject, says: item.says ?? null };
}
