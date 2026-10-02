/** The client against the real server: `python -m claude_human task serve` on a free loopback
 *  port, a temporary SQLite store and a test token. Tasks are opened through the command line and
 *  read back through the client. Skipped when Python or the claude_human package is missing. */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createTaskClient, TaskApiError, type TaskClient } from "../src";

const PYTHON = process.env.CLAUDE_HUMAN_PYTHON || "python3";
const TOKEN = "test-token-not-a-secret";

function havePackage(): boolean {
  const r = spawnSync(PYTHON, ["-c", "import claude_human.tasks.server"], { encoding: "utf8" });
  return r.status === 0;
}

const available = havePackage();

if (!available && process.env.CLAUDE_HUMAN_REQUIRE_SERVER === "1") {
  throw new Error(`CLAUDE_HUMAN_REQUIRE_SERVER is set and ${PYTHON} cannot import claude_human`);
}

/** The environment for every Python call: never a real notification, and the test token. */
function childEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const k of Object.keys(env)) {
    if (k.startsWith("CLAUDE_HUMAN_")) delete env[k];
  }
  env.CLAUDE_HUMAN_NOTIFY_HOLD = "1";
  env.CLAUDE_HUMAN_TASKS_TOKEN = TOKEN;
  return env;
}

describe.skipIf(!available)("against claude-human task serve", () => {
  let dir = "";
  let db = "";
  let server: ChildProcess | undefined;
  let client: TaskClient;

  function cli(...args: string[]): Record<string, unknown> {
    const r = spawnSync(PYTHON, ["-m", "claude_human", "task", "--db", db, ...args],
                        { encoding: "utf8", env: childEnv() });
    if (r.status !== 0) throw new Error(`task ${args[0]} failed: ${r.stderr || r.stdout}`);
    return JSON.parse(r.stdout);
  }

  beforeAll(async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "claude-human-client-"));
    db = path.join(dir, "tasks.db");
    server = spawn(PYTHON, ["-m", "claude_human", "task", "--db", db, "serve", "--port", "0"],
                   { env: childEnv(), stdio: ["ignore", "pipe", "pipe"] });
    const base = await new Promise<string>((resolve, reject) => {
      let out = "";
      let err = "";
      const timer = setTimeout(() => reject(new Error(`the server did not start: ${out}${err}`)), 15000);
      server!.stdout!.on("data", (b: Buffer) => {
        out += b.toString();
        const m = /tasks on (http:\/\/127\.0\.0\.1:\d+)/.exec(out);
        if (m) {
          clearTimeout(timer);
          resolve(m[1]);
        }
      });
      server!.stderr!.on("data", (b: Buffer) => { err += b.toString(); });
      server!.on("exit", (code) => {
        clearTimeout(timer);
        reject(new Error(`the server exited with ${code}: ${err}`));
      });
    });
    client = createTaskClient({ baseUrl: base, token: TOKEN, supports: ["steps", "url"],
                                platform: "android" });
  }, 20000);

  afterAll(() => {
    server?.kill();
    if (dir) fs.rmSync(dir, { recursive: true, force: true });
  });

  it("answers health only with the token", async () => {
    await expect(client.health()).resolves.toEqual({ ok: true });
    const wrong = createTaskClient({ baseUrl: client.baseUrl, token: "wrong" });
    const err = await wrong.health().catch((e) => e);
    expect(err).toBeInstanceOf(TaskApiError);
    expect(err.status).toBe(401);
  });

  it("reads back a task opened through the command line, and closes it only on the check", async () => {
    const flag = path.join(dir, "approved");
    const opened = cli("open", "approve", "app://demo", "--reason", "The demo needs approval.",
                       "--steps", "Open the demo and press Allow.",
                       "--handoff", "url=https://example.org/demo",
                       "--", PYTHON, "-c",
                       `import os, sys; sys.exit(0 if os.path.exists(${JSON.stringify(flag)}) else 1)`);
    const id = String(opened.id);
    expect(opened.new).toBe(true);

    const pending = await client.pending();
    const t = pending.find((x) => x.intent === id);
    expect(t).toBeDefined();
    expect(t!.verb).toBe("approve");
    expect(t!.subject).toBe("app://demo");
    expect(t!.reason).toBe("The demo needs approval.");
    expect(t!.handoffs.map((h) => h.kind)).toEqual(["url", "steps"]);
    expect(t!.head).toMatchObject({ kind: "url", target: "https://example.org/demo" });
    expect(t!.requires).toBe("remote");

    const one = await client.task(id);
    expect(one.intent).toBe(id);
    expect(one.handoffs.at(-1)).toMatchObject({ kind: "steps", target: "Open the demo and press Allow." });

    // a reader that can only show words gets the steps and no head
    const wordsOnly = createTaskClient({ baseUrl: client.baseUrl, token: TOKEN });
    const plain = await wordsOnly.task(id);
    expect(plain.handoffs.map((h) => h.kind)).toEqual(["steps"]);
    expect(plain.head).toBeNull();

    await expect(client.open(id)).resolves.toMatchObject({
      kind: "url", target: "https://example.org/demo", prepared: null,
    });

    const first = await client.done(id);
    expect(first.done).toBe(false);
    expect(first.still_pending).toBe(true);

    fs.writeFileSync(flag, "yes");
    const second = await client.done(id);
    expect(second).toMatchObject({ done: true, still_pending: false });
    expect((await client.pending()).some((x) => x.intent === id)).toBe(false);

    const closed = await client.task(id);
    expect(closed.state).toBe("done");
    expect(closed.outcome).toBe("success");

    const history = await client.history(20);
    expect(history.find((h) => h.intent === id && h.verb === "task.done")).toMatchObject({
      outcome: "success", origin: "observed", subject: "app://demo",
    });

    const late = await client.withdraw(id, "too late");
    expect(late.ok).toBe(false);
  });

  it("records comments and withdrawals with the person's words", async () => {
    const opened = cli("open", "plug-in", "device://drive", "--reason", "Plug the drive in.");
    const id = String(opened.id);

    await expect(client.comment(id, "")).resolves.toMatchObject({ ok: false });
    const c = await client.comment(id, "It is in the drawer.");
    expect(c).toMatchObject({ ok: true, subject: "device://drive", intent: id });
    expect((await client.comments()).some((x) => x.intent === id && x.text === "It is in the drawer.")).toBe(true);

    const w = await client.withdraw(id, "not needed any more");
    expect(w).toMatchObject({ ok: true, reason: "not needed any more" });
    const history = await client.history(10);
    expect(history.find((h) => h.intent === id && h.verb === "task.withdraw")?.says).toBe("not needed any more");
    expect((await client.task(id)).state).toBe("withdrawn");
  });

  it("answers 404 for a task that does not exist", async () => {
    const err = await client.task("no-such-task").catch((e) => e);
    expect(err).toBeInstanceOf(TaskApiError);
    expect(err.status).toBe(404);
  });
});

describe.skipIf(available)("against claude-human task serve (skipped)", () => {
  it.skip(`needs ${PYTHON} with the claude_human package`, () => {});
});
