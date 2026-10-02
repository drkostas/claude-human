/** Small pure helpers a reader of the task server needs. No React, no React Native. */
import type { Handoff, PendingTask } from "./types";

/** The kinds that are words and not a surface. The chain ends in one of them when the reader can
 *  show it. */
export const FLOOR_KINDS: readonly string[] = ["steps"];

/** "just now", "waiting 12m" or "waiting 3h" from a count of seconds, and "" when it is unknown. */
export function waitingLabel(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `waiting ${m}m`;
  const h = Math.floor(m / 60);
  return `waiting ${h}h`;
}

/** Whether a handoff is the words (the floor) rather than a surface to open. */
export function isFloor(h: Pick<Handoff, "kind">, floor: readonly string[] = FLOOR_KINDS): boolean {
  return floor.includes(h.kind);
}

/** The steps the person can always follow: the floor entry of the chain, or the task's own steps
 *  when the chain has none. null when the task has no words at all. */
export function floorSteps(task: Pick<PendingTask, "handoffs" | "steps">,
                           floor: readonly string[] = FLOOR_KINDS): string | null {
  const entry = (task.handoffs ?? []).find((h) => isFloor(h, floor) && h.target);
  return entry?.target ?? task.steps ?? null;
}
