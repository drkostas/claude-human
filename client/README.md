# @drkostas/claude-human-client

A typed TypeScript client for the claude-human task server (`claude-human task serve`). An app on a phone uses it to list the tasks waiting on a person, show one task with its handoff chain, prepare the first surface, ask the server to run the check, and record comments and withdrawals.

It has no dependencies. It runs anywhere `fetch` does (React Native, a browser, Node 18 and later).

## Install

```bash
npm install @drkostas/claude-human-client
```

## Use

```ts
import { createTaskClient, waitingLabel } from "@drkostas/claude-human-client";

const tasks = createTaskClient({
  baseUrl: "https://tasks.example.org",   // the server, behind TLS and access control
  token: process.env.EXPO_PUBLIC_TASKS_TOKEN,
  supports: ["steps", "url"],              // what this app can show
  platform: "android",                     // where it runs
  retries: 1,                              // a failed GET is tried once more
});

const pending = await tasks.pending();     // PendingTask[], each with its ordered chain and head
const task = await tasks.task(pending[0].intent);
const ready = await tasks.open(task.intent);         // prepares the head of the chain
const answer = await tasks.done(task.intent);        // runs the check, never closes by itself
await tasks.comment(task.intent, "The setting is not where the steps say.");
await tasks.withdraw(task.intent, "Not needed any more.");
const history = await tasks.history(50);
```

Nothing is hardcoded. The base URL and the token are passed in. The token can be a function, which the client calls at each request, so a new token takes effect without a new client. An empty token sends no `Authorization` header.

| call | route | answers |
|---|---|---|
| `health()` | `GET /health` | `{ ok }` |
| `pending()` | `GET /pending?supports=..&platform=..` | `PendingTask[]` |
| `task(id)` | `GET /task/<id>?supports=..&platform=..` | `PendingTask` |
| `history(limit)` | `GET /history?limit=..` | `HistoryItem[]` |
| `comments()` | `GET /comments` | `CommentItem[]` |
| `open(id)` | `POST /open/<id>?supports=..&platform=..` | `OpenResult` |
| `done(id)` | `POST /done/<id>` | `DoneResult` |
| `comment(id, text)` | `POST /comment/<id>` | `CommentResult` |
| `withdraw(id, reason)` | `POST /withdraw/<id>` | `WithdrawResult` |

`getJson(path)` and `postJson(path, body)` reach other routes on the same server with the same headers, and `headers()` returns them (for a WebView, for example).

## What the answers mean

- The server orders the chain for the reader and sends `head`, the first handoff that is not the steps. Show the head. Do not rank the chain again in the app, or two readers can disagree about what to open.
- The steps are the floor. They are the last entry of the chain whenever the reader supports `steps`, so a reader that can show only words always has something to do. `floorSteps(task)` returns them.
- `done(id)` asks the server to run the task's check. `done: true` means the check passed. `still_pending: true` means it did not, and `detail` says what the check saw, in words to show the person.
- `open(id)` answers `prepared: null` when the surface needed nothing and `prepared: false` when it could not be prepared. Do not show a surface as ready after `false`.
- `comment` and `withdraw` answer `ok: false` with a reason when the server refuses (an empty comment, a task that is already closed). That is an answer to show, not an error.
- A status other than 2xx throws `TaskApiError`. Its message is `HTTP <status>`, and it carries `status` and the parsed `body`. A failed GET is tried again only when `retries` is set, and never after a 4xx. A POST is never repeated.

## A server with more fields

A program that runs its own server with the same routes can send more fields. Extend the type and pass it to the call.

```ts
import type { PendingTask } from "@drkostas/claude-human-client";

interface MyTask extends PendingTask { risk: "low" | "high" }
const mine = await tasks.pending<MyTask>();
```

## Tests

```bash
npm ci
npm test
```

The unit tests run the client against a fake `fetch`. One test starts `python3 -m claude_human task serve` on a free loopback port with a temporary task file and a test token, opens tasks through the command line, and reads them back through the client. It is skipped when Python or the `claude_human` package is missing. `CLAUDE_HUMAN_PYTHON` picks another interpreter.

## License

MIT
