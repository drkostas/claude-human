# Changelog

## Unreleased

- `@drkostas/expo-ntfy` 0.2.1: `pollTopicOnce` gives up after `timeoutMs` (default 20 seconds, `POLL_TIMEOUT_MS`). It aborts the fetch through an `AbortSignal` and also stops waiting for a fetch that ignores the signal, and it then rejects without announcing anything or moving the mark. Before, a topic that never answered kept the background task pending, and Android's WorkManager starts no other run of a task while one is pending, so one hung connection stopped every later background poll.
- `screenshot.activate` no longer brings Finder back with `open -b`. To a running app that is a reopen, and Finder answers a reopen with no window open by opening a new window at its default folder on the visible desktop, so giving focus back after another app took it (Finder is in front when the desktop itself has focus) made Finder windows appear. Such apps are listed in `screenshot.NO_REOPEN`, and `activate` returns False for them without asking
- CI compiles the Android service the expo-ntfy plugin writes (`npm run compile:service` in js/), against the Android 36 platform and the libraries the plugin adds, so a service that does not compile fails a pull request instead of an app's build.
- `screenshot.capture_approvals()` and `capture_approval_of()`: when macOS last asked each app to confirm that it may keep recording the screen, and when it asks again (macOS 15 and later ask monthly for an app that records without the system picker). Read only, from the system's own file, with the dates in UTC.
- The station's refusal while a system dialog is up carries the dialog's app as `blocker`, and `QuartzInput(blocked_detail=...)` may be a function of that name, so the text can say which question is waiting.
- `@drkostas/expo-ntfy` 0.2.0: a message's `view` actions become buttons on the notification, up to three, each opening its link in the app like a tap (and only a link that starts with `linkPrefix`). The priority picks one of three channels. At `alarmPriority` (default 5) the message plays the phone's alarm sound as an alarm and comes through Do Not Disturb where Android allows it. At `chimePriority` (default 4) it chimes on the channel messages were always posted on. Below that it makes no sound. A message published with ntfy's default priority (3) is now silent, so set `chimePriority: 3` to keep its sound. A notification folds to its title and the first line of the message, and pulls down to the whole message. `subTextTagPrefix` shows a tag's value next to the app's name. The manifest asks for `ACCESS_NOTIFICATION_POLICY`, so the person can give the app Do Not Disturb access.
- `@drkostas/expo-ntfy` 0.2.0 also carries the fix written down for 0.1.2, which was never published. Tapping a notification that carries a link now opens that link. The service built the tap from the app's launch intent with the link attached, and the launch intent carries `CATEGORY_LAUNCHER`, so when the app was already running Android only brought its task to the front and dropped the link: the tap opened the app on whatever screen it was last on. A link now opens as a `VIEW` intent aimed at the app's package (new task, single top), which a running app receives as a new intent. A notification without a link still opens the app. The service can also be started from outside without opening the app, with a `<package>.START_LISTENER` broadcast to the receiver (`adb shell am broadcast -n <package>/.BootReceiver -a <package>.START_LISTENER`), for a phone that did not restart it after an update.
- `screenshot.screen_locked()` and `unlock.is_locked()` return `None` when the lock state cannot be read. A process outside the login session (a launchd daemon, the system domain) gets no session dictionary, and both functions used to read that as unlocked while the screen was locked. A caller that only tests truthiness behaves as before. `is_locked` now uses the same reader as `screen_locked`.
- The two npm packages are released from GitHub Actions, `@drkostas/expo-ntfy` on an `expo-ntfy-v<version>` tag and `@drkostas/claude-human-client` on a `client-v<version>` tag. The workflow attaches the package's `npm pack` tarball to a GitHub release of the tag, which installs by URL. When the repository variable `NPM_PUBLISH` is `true` it also publishes the same tarball to npm with npm's trusted publishing, so no npm token is kept anywhere and the release carries provenance.

## 0.6.0

- Security: without sckshot, capturing one window cropped the whole display to the window's rectangle, so whatever covered the window came through instead of it, and the station streamed that to a grant pinned to one app or one window. The capture now refuses a window that another application's window lies in front of and overlaps, checked before and after the grab, and returns nothing, as it already did for a window it cannot find. System chrome that draws no other application's content (the Dock, the menu bar) and windows with almost no opacity do not count, and Notification Center's banners do. Reported by Dennis. The sckshot path captures the window itself and was not affected.
- `claude-human python-app` builds a signed `.app` that runs Python, so a long-running Python program keeps its Screen Recording and Accessibility grants across Python upgrades (Homebrew signs Python.app ad hoc, and a grant given to it is lost at every upgrade)
- `screenshot.window_ids()`, the id of every window on every desktop, and `screenshot.activate(bundle_id)`, which brings an app to the front with `open -b` and needs no Automation grant

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
