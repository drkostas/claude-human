# Changelog

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
