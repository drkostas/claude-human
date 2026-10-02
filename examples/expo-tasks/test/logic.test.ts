import { describe, expect, it } from "vitest";
import type { PendingTask } from "@drkostas/claude-human-client";

import {
  canOpen, chainRows, doneMessage, historyLine, openProblem, pendingLine, routeFor, taskSections,
} from "../src/logic";

const P = "expotasks://";

const TASK: PendingTask = {
  intent: "12",
  subject: "app://backup",
  verb: "approve",
  tier: "confirm",
  reason: "The backup needs Full Disk Access.",
  steps: "Open System Settings, Privacy, Full Disk Access, and turn on Backup.",
  waiting_s: 300,
  handoffs: [
    { kind: "settings", target: "x-settings://privacy", label: null, preference: 10, platform: "any" },
    { kind: "url", target: "https://example.org/help", label: "Read the help page", preference: 20, platform: "any" },
    { kind: "steps", target: "Open System Settings, Privacy, Full Disk Access, and turn on Backup.",
      label: "Follow the steps", preference: 9999, platform: "any" },
  ],
  head: { kind: "settings", target: "x-settings://privacy", label: null, preference: 10, platform: "any" },
};

describe("routeFor", () => {
  it("opens a task from its link", () => {
    expect(routeFor("expotasks://task/12", P)).toEqual({ name: "task", id: "12" });
    expect(routeFor("expotasks://task/a%2Fb?from=tray", P)).toEqual({ name: "task", id: "a/b" });
  });
  it("knows the list and the history", () => {
    expect(routeFor("expotasks://", P)).toEqual({ name: "pending" });
    expect(routeFor("expotasks://pending", P)).toEqual({ name: "pending" });
    expect(routeFor("expotasks://history", P)).toEqual({ name: "history" });
  });
  it("never follows a link that is not the app's own", () => {
    expect(routeFor("https://example.org/task/12", P)).toBeNull();
    expect(routeFor("otherapp://task/12", P)).toBeNull();
    expect(routeFor(undefined, P)).toBeNull();
    expect(routeFor("expotasks://task/12/extra", P)).toBeNull();
    expect(routeFor("expotasks://task/%E0%A4%A", P)).toBeNull();
  });
});

describe("the task page", () => {
  it("says why the person is asked and what to do", () => {
    expect(taskSections(TASK)).toEqual({
      title: "approve: app://backup",
      whyYou: "The backup needs Full Disk Access.",
      whatToDo: "Open System Settings, Privacy, Full Disk Access, and turn on Backup.",
    });
    const bare = taskSections({ ...TASK, reason: null, steps: null, handoffs: [] });
    expect(bare.whyYou).toBe("No reason was given.");
    expect(bare.whatToDo).toMatch(/no written steps/);
  });

  it("keeps the server's order, marks the head, and ends with the floor", () => {
    const rows = chainRows(TASK);
    expect(rows.map((r) => r.title)).toEqual(["Open the settings", "Read the help page", "Follow the steps"]);
    expect(rows.map((r) => r.isHead)).toEqual([true, false, false]);
    expect(rows.map((r) => r.isFloor)).toEqual([false, false, true]);
    expect(rows.map((r) => r.openable)).toEqual([true, true, false]);
    expect(rows[2].detail).toMatch(/Always works/);
  });

  it("has no head when the steps are all there is", () => {
    const rows = chainRows({ handoffs: [TASK.handoffs[2]], head: null });
    expect(rows).toHaveLength(1);
    expect(rows[0].isHead).toBe(false);
    expect(rows[0].isFloor).toBe(true);
  });

  it("opens only the kinds it can, with a target", () => {
    expect(canOpen({ kind: "url", target: "https://example.org" })).toBe(true);
    expect(canOpen({ kind: "url", target: null })).toBe(false);
    expect(canOpen({ kind: "vnc", target: "vnc://x" })).toBe(false);
  });

  it("lets the check decide, not the button", () => {
    expect(doneMessage({ done: true, still_pending: false })).toEqual({ tone: "done", text: "Checked, and it is done." });
    expect(doneMessage({ done: false, still_pending: true, detail: "Checked, and it is not done yet. (exit 1)" }))
      .toEqual({ tone: "waiting", text: "Checked, and it is not done yet. (exit 1)" });
  });

  it("never shows a surface that could not be prepared as ready", () => {
    expect(openProblem({ kind: "url", target: "https://x", prepared: false, detail: "not prepared: exit 2" }))
      .toBe("It is not ready: not prepared: exit 2");
    expect(openProblem({ kind: null, target: null, prepared: null, detail: "only steps" })).toMatch(/Follow the steps/);
    expect(openProblem({ kind: "url", target: "https://x", prepared: null, detail: "nothing to prepare" })).toBeNull();
    expect(openProblem({ kind: "url", target: "https://x", prepared: true, detail: "prepared" })).toBeNull();
  });
});

describe("lists", () => {
  it("shows how long a task has waited", () => {
    expect(pendingLine(TASK)).toBe("app://backup, waiting 5m");
    expect(pendingLine({ subject: "x", waiting_s: null })).toBe("x");
  });

  it("reads history in words", () => {
    expect(historyLine({ at: "t", verb: "task.withdraw", capability: "approve", subject: "app://backup",
                         outcome: "withdrawn", says: "not needed" }))
      .toEqual({ title: "Dropped", subject: "approve app://backup", says: "not needed" });
    expect(historyLine({ at: "t", verb: "task.custom", subject: null, outcome: null }).title).toBe("task.custom");
  });
});
