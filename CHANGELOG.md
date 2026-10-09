# Changelog

## 0.5.0

- Every function that starts a process takes an optional `env` for it: `unlock`, `relock`, `use_password`, `approve`, `screenshot.capture` and `save`, `yabai_windows`, and the classes `CommandVerifier`, `DefaultResolver` and `station.prepare.Preparer`. `None` keeps the inherited environment. With an env, the preparer also looks for yabai on that env's PATH
- `@drkostas/expo-ntfy` 0.1.1: the listener is a `specialUse` foreground service instead of `dataSync`. Android 15 stops a `dataSync` service after 6 hours in any 24 and refuses to restart it until the app is opened, so notifications stopped every afternoon. The plugin also rewrites an existing service entry and drops the old permission, so a build without `--clean` is fixed too.
- `@drkostas/claude-human-client` 0.1.0 in `client/`, a typed TypeScript client for `claude-human task serve`. It has a call for each route (`pending`, `task`, `open`, `done`, `comment`, `withdraw`, `history`, `comments`, `health`), the types of their JSON, `TaskApiError` for a status other than 2xx, retries for GETs only, and the helpers `waitingLabel`, `floorSteps` and `touchpointQuery`. The base URL and the token are passed in, and the token can be a function read at each request. Its tests run against a fake `fetch` and against the real server started on a free loopback port.
- `examples/expo-tasks`, an Expo app built on the client and `@drkostas/expo-ntfy`, with a list of waiting tasks, a task page and the history.
- CI typechecks and tests the client and the example app.
- The skill has a section on reading tasks from an app.

## 0.4.0

- `claude_human.tasks`, a task engine for the steps only a person can do. `TaskEngine.request` keeps one open task per capability and subject (a second request joins the first and nobody is told twice), asks an `Authorizer` and records a refusal before raising `Refused`, and tells the person through any `claude_human.notify` notifier with a link to the task. `verify_pending` closes a task only when its check passes, and a check that fails, is missing or times out leaves it open.
- `CommandVerifier` runs the check as a list of arguments with no shell and a timeout. `filter_chain` and `DefaultResolver` order the handoffs (link, station window, steps) for what a reader can show and where it runs, with the steps as the floor, and `head` is the surface to render first.
- `SqliteTaskStore`, the default `TaskStore`, in one SQLite file. Open or closed is derived from append only events, and comments, withdrawals, refusals and the notifier's answer are kept in the history.
- `claude_human.tasks.server`, an HTTP server on a loopback address with a bearer token, with the routes and JSON shapes a phone app reads (`/pending`, `/task`, `/history`, `/comments`, `/done`, `/open`, `/comment`, `/withdraw`).
- `claude-human task open|list|verify|withdraw|comment|history|serve`.
- `station.auth.load_or_create_token` takes a `prefix` for the token it makes.
- The skill has a new section on asking a person to do something.

## 0.3.0

- `claude_human.notify`, a notifier for a person through an ntfy server, with the standard library only. `NtfyNotifier` publishes JSON (title, message, priority, tags, click link, picture, action buttons, Markdown) with a bearer token or basic auth and a timeout, and returns `(ok, detail)` instead of raising on a network failure. `poll` reads back what the server holds. `FileNotifier` writes each message to a JSON lines file as the floor, `FirstThatWorks` tries notifiers in order, and all of them follow the `Notifier` protocol.
- A message whose link, picture or button URL is on a loopback address is refused, because the phone would open itself. `CLAUDE_HUMAN_NOTIFY_HOLD` stops every send, for test runs. `since` values in days are turned into hours for ntfy.
- `watch_sender`, which lets `claude_ops.watch.run_source` tell a person instead of a chat.
- `claude-human notify`, which reads the token or password from the environment only.
- The skill has new sections on sending notifications and on writing a watcher that tells a person or wakes a chat.

## 0.2.0

- `claude_human.station`, a web server that shows one window of the Mac on a phone and sends the person's taps and keys back as clicks and key presses. It listens on 127.0.0.1 only, reads the token from the `Authorization` header only, and keeps a pinned grant to its own app's window (a window it cannot find is refused, never widened to the whole display).
- `StationAuth`, the interface a caller implements to decide who may use the station (`check`, `app_for`) and to record what happens (`perform`, `on_input`). `TokenAuth` is the default, one shared token from the environment or a file made with mode 0600.
- `claude_human.station.prepare`, the recipe runner that puts a window into the state a task needs (`open`, `wait`, `search`, `type`, `key`, `click`, `scroll`, `activate`) in a placement phase and an input phase.
- `claude-human station` and `claude-human prepare` commands.

## 0.1.0

First release.

- `claude_human.screenshot` with the `sckshot` ScreenCaptureKit tool, window listing, and lock and password panel detection
- `claude_human.unlock` with the `vhid_type` virtual HID keyboard helper, for the lock screen and SecurityAgent password panels
- `claude-human build-tools`, which takes the signing identity and bundle id from options or the environment
- `@drkostas/expo-ntfy` 0.1.0 in `js/`, the Expo config plugin for a native ntfy foreground service on Android, with the wire logic and the notification glue
