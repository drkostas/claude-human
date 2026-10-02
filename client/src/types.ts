/** The JSON shapes of the task server (`claude-human task serve`, claude_human/tasks/server.py).
 *
 *  Each type names the fields the server sends. A program that runs its own server with more fields
 *  extends these types and passes the extended type to the client call as its type argument. */

/** What a reader can show. The server sends only kinds the reader said it supports. The named ones
 *  are the kinds the claude-human server and its station know about, and any other string is
 *  allowed for a server with kinds of its own. */
export type HandoffKind = "steps" | "url" | "settings" | "vnc" | "shell" | (string & {});

/** One way to put the person in front of the thing a task is about. */
export interface Handoff {
  kind: HandoffKind;
  /** The address to open, or the text of the steps. */
  target: string | null;
  label: string | null;
  /** Lower comes first. The server has already sorted the chain by it. */
  preference: number;
  /** `any`, or the one platform this handoff works on. */
  platform: string;
}

/** One open task, as GET /pending and GET /task/<id> send it. */
export interface PendingTask {
  /** The task id. */
  intent: string;
  subject: string;
  /** What the person is asked to do (the capability). */
  verb: string;
  tier: string;
  reason: string | null;
  steps: string | null;
  waiting_s: number | null;
  /** The ordered chain for this reader. The steps come last, as the floor. */
  handoffs: Handoff[];
  /** The surface to show first, chosen by the server: the first handoff that is not the steps.
   *  null when the steps are all there is. A reader shows this and does not rank the chain again. */
  head: Handoff | null;
  /** `remote` when some surface other than the steps exists, else `physical`. */
  requires?: string;
  owner?: string | null;
  /** `open`, `done` or `withdrawn`. */
  state?: string;
  opened_at?: string | null;
  closed_at?: string | null;
  outcome?: string | null;
}

/** One line of GET /history, newest first. */
export interface HistoryItem {
  id?: number | string;
  at: string;
  /** The event, such as `task.open`, `task.done`, `task.comment` or `task.withdraw`. */
  verb: string;
  capability?: string | null;
  subject: string | null;
  outcome: string | null;
  /** `observed` when a check saw the task done. */
  origin?: string | null;
  actor?: string | null;
  intent?: string | null;
  /** The one line a person wants to read: the words of a comment or the reason for a withdrawal. */
  says?: string | null;
  detail?: Record<string, unknown> | null;
}

/** One comment, from GET /comments. */
export interface CommentItem {
  id: number | string;
  at: string;
  intent: string | null;
  subject: string | null;
  said_by: string | null;
  text: string | null;
}

/** What POST /done/<id> answers. Pressing "done" asks for the check and does not answer it, so
 *  `done` is true only when the check passed. */
export interface DoneResult {
  done: boolean;
  still_pending: boolean;
  /** What the check saw, in words to show the person. */
  detail?: string;
}

/** What POST /open/<id> answers. `prepared` is null when the surface needed nothing, and false when
 *  it could not be prepared (then `detail` says why, and the reader must not show it as ready). */
export interface OpenResult {
  kind: string | null;
  target: string | null;
  prepared: boolean | null;
  detail: string;
}

/** What POST /comment/<id> answers. `ok: false` with a `detail` is a refusal to show the person
 *  (an empty comment is not recorded), not an error. */
export interface CommentResult {
  ok: boolean;
  detail?: string;
  comment?: number | string;
  subject?: string;
  intent?: string;
}

/** What POST /withdraw/<id> answers. `ok: false` carries the reason (no such task, already
 *  closed). */
export interface WithdrawResult {
  ok: boolean;
  status?: string;
  outcome?: string;
  reason?: string | null;
  event?: number | string;
  detail?: Record<string, unknown>;
}

export interface HealthResult {
  ok: boolean;
}
