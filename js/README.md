# @drkostas/expo-ntfy

This package gets messages from a self-hosted [ntfy](https://ntfy.sh) server to an Android phone running an Expo app, without Firebase. I wrote it because the JavaScript socket in my app stopped receiving the moment the phone locked, so notifications only reached a phone that someone was already looking at.

It has three parts.

- A config plugin that adds a native Android foreground service. The service holds the ntfy WebSocket while the app is closed and posts every message to the tray itself.
- The wire logic (`proto` and `poll`), with no Expo or React Native imports, so it runs in Node and is tested there.
- The Expo glue (`notify` and `background`) for permissions, local notifications, the socket while the app is open, taps, and a background poll.

## Install

Each release is attached to the GitHub release of its tag, `expo-ntfy-v<version>`, and installs from there by URL:

```bash
npm install https://github.com/drkostas/claude-human/releases/download/expo-ntfy-v0.2.0/drkostas-expo-ntfy-0.2.0.tgz
```

Versions up to 0.1.1 are also on npm (`npm install @drkostas/expo-ntfy`).

It expects `expo`, `expo-notifications`, `react` and `react-native` in the app, and `expo-background-task` and `expo-task-manager` if you use the background poll.

## The config plugin

```json
{
  "expo": {
    "scheme": "myapp",
    "android": { "package": "org.example.myapp" },
    "plugins": [
      ["@drkostas/expo-ntfy", { "alertChannelName": "My app" }]
    ]
  }
}
```

The topic URL comes from `EXPO_PUBLIC_NTFY_URL` at prebuild time, or from the `url` option. There is no default address. The Android prebuild stops with an error when the URL is missing, is not http(s), or is a loopback address (localhost, 127.0.0.1, ::1), because a phone given that address connects to itself and never receives anything. Reading the app config (`expo start`, a web build) does not need the URL.

| option | default |
|---|---|
| `url` | the value of `urlEnv` |
| `urlEnv` | `EXPO_PUBLIC_NTFY_URL` |
| `package` | `expo.android.package` |
| `buildConfigField` | `NTFY_TOPIC_URL` |
| `token` | the value of `tokenEnv`, or none |
| `tokenEnv` | `EXPO_PUBLIC_NTFY_TOKEN` |
| `tokenBuildConfigField` | `NTFY_TOKEN` |
| `startReportUrl` | the value of `startReportUrlEnv`, or no start report |
| `startReportUrlEnv` | `EXPO_PUBLIC_NTFY_START_REPORT_URL` |
| `startReportUrlBuildConfigField` | `NTFY_START_REPORT_URL` |
| `startReportToken` | the value of `startReportTokenEnv`, or none |
| `startReportTokenEnv` | `EXPO_PUBLIC_NTFY_START_REPORT_TOKEN` |
| `startReportTokenBuildConfigField` | `NTFY_START_REPORT_TOKEN` |
| `startReportEveryMinutes` | `0`, which reports only when the socket opens |
| `defaultTitle` | `expo.name` |
| `linkPrefix` | `<expo.scheme>://`, or no links when there is no scheme |
| `alertChannelId`, `alertChannelName`, `alertChannelDescription` | `ntfy_messages`, `expo.name`, a short text |
| `alarmChannelId`, `alarmChannelName`, `alarmChannelDescription` | `ntfy_alarm`, `Urgent`, a short text |
| `silentChannelId`, `silentChannelName`, `silentChannelDescription` | `ntfy_silent`, `Quiet`, a short text |
| `alarmPriority`, `chimePriority` | `5`, `4` |
| `subTextTagPrefix` | empty, so no tag is shown |
| `ongoingChannelId`, `ongoingChannelName`, `ongoingChannelDescription` | `ntfy_connection`, `Connection`, a short text |
| `ongoingTitle`, `ongoingText` | `<expo.name> is connected`, `waiting for messages` |
| `logTag` | `NtfyService` |

What it writes at prebuild.

- `NtfyService.kt` and `BootReceiver` next to `MainActivity`, in the app's package.
- The URL and the access token as BuildConfig fields (the token field is empty when there is none). Both are replaced on every build, so building again corrects a wrong one.
- A call in `MainActivity.onCreate` that starts the service.
- The manifest entries for the service (type `specialUse`), the receiver, and the permissions `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `RECEIVE_BOOT_COMPLETED`, `WAKE_LOCK` and `ACCESS_NOTIFICATION_POLICY`.

### Telling a server that the phone started

A phone that restarts leaves nothing on the network to say so. With a start report URL, the service posts to it each time its socket opens, which after a restart is as soon as the phone is on a network, and every `startReportEveryMinutes` while the socket stays open:

```json
{"uptime_s": 4521.3, "why": "connected"}
```

`uptime_s` is how long the phone has run in seconds, counting the time it slept (`SystemClock.elapsedRealtime`). Subtract it from your own clock to get the moment the phone started. Using the receiver's clock means a phone whose own clock is wrong still reads right, and a moment that moved by more than the report's delay means the phone restarted. `why` is `connected` for a report sent when the socket opened and `again` for a scheduled one. The request carries `Authorization: Bearer <token>` when `startReportToken` or `EXPO_PUBLIC_NTFY_START_REPORT_TOKEN` is set. Like the ntfy token it is part of the app you ship, so give it only the right to make this report. A report that fails is logged and dropped, since the next one is the retry. With no URL, nothing is sent and the build fields are empty.

### A server that requires authentication

An ntfy server started with `--auth-default-access deny-all` answers a subscriber without credentials with 401. Give the app an access token (`ntfy token add <user>` on the server) through `EXPO_PUBLIC_NTFY_TOKEN` at prebuild time, or the `token` option. The service then sends `Authorization: Bearer <token>` when it opens the socket, never the token in the URL. `pollTopicOnce` and `useNtfy` take the same token as their `token` option, so the background poll and the foreground socket authenticate too.

The token is part of the app you ship, so anyone holding the APK can read it. Give the app a user that may only read its topics (`ntfy access <user> <topic> read-only`), and publish with a different token.

A message's click URL opens the app only when it starts with `linkPrefix`. A notification is data from the network, so any other link is shown in the text but never followed.

## What a message shows

- Folded, on the lock screen and in the shade, a notification shows the message's title and the first line of its text. Pulled down, it shows the whole text and its buttons.
- Each `view` action becomes a button, up to three, in the order they came. A button opens its URL in the app the same way a tap opens `click`, and only when the URL starts with `linkPrefix`. `http` and `broadcast` actions are not drawn. A button whose action says `"clear": true` removes the notification and then opens its URL, as in ntfy's own app. It goes through a small activity of the plugin's (`NtfyTapActivity`, not exported, with no window), because Android 12 and later do not let a receiver or service started from a notification open an activity, while an activity may. A button without `clear` opens its URL and leaves the notification in the tray, since Android removes a notification by itself only when its body is tapped.
- The priority picks one of three channels. At `alarmPriority` or above (5 by default) the message rings the alarm channel, which plays the phone's alarm sound as an alarm. At `chimePriority` or above (4) it chimes on the alert channel, which keeps the id messages were posted on before there were three channels. Below that it is silent, in the tray with no sound. ntfy's default priority is 3, so set `chimePriority` to 3 if a message published without a priority should still make a sound.
- Do Not Disturb lets the alarm through wherever it lets alarms through. The alarm channel also asks to bypass Do Not Disturb, and Android keeps that only when the app has Do Not Disturb access, which the person grants in the system settings. The app is listed there because the manifest asks for `ACCESS_NOTIFICATION_POLICY`.
- With `subTextTagPrefix` set (for example `"area:"`), the first tag that starts with it is shown, without the prefix, next to the app's name in the notification's header.
- Android fixes a channel's sound when the channel is created. The person can still change each channel's sound in the app's notification settings.

## Secrets the JavaScript reads, kept out of its bundle

A token read from `process.env.EXPO_PUBLIC_...` is copied into the JavaScript bundle at build time. When the app updates its JavaScript over the air (expo-updates), every update is then a file that carries the token, and it has to carry the token of the build that is installed. The `build-secrets` plugin keeps such secrets in the native part instead:

```json
["@drkostas/expo-ntfy/build-secrets", { "secrets": { "apiToken": "MY_APP_API_TOKEN" } }]
```

```ts
import { buildSecret } from "@drkostas/expo-ntfy/secrets";
const token = buildSecret("apiToken");   // undefined on the web, or when the build wrote none
```

At prebuild it writes each secret into `android/app/src/main/res/values/build_secrets.xml` (mode 0600, kept from the resource shrinker), and a small Expo module of this package reads it by name. The options name environment variables, never values, so the app config, which an exported update carries, holds no secret. A variable that is unset or empty fails the prebuild. The secrets are still part of the APK, as a BuildConfig field would be, so give each one only what that build needs.

### A token for the update server

An update server that wants a token from the app gets it from expo-updates' request headers. Those come from `updates.requestHeaders` in the app config, which an exported update carries and which the fingerprint of the runtime version reads. A token there is published with every update, and gives every build its own runtime version. Replacing the header at run time with `Updates.setUpdateRequestHeadersOverride` has its own cost. expo-updates keeps the override and from then on launches only the updates it fetched with the same headers, while the update embedded in the APK keeps the build's own. So after the first start every start is an emergency launch of the embedded code.

`updatesRequestHeaders` writes the header into the Android manifest instead, where expo-updates reads the build's own headers.

```json
["@drkostas/expo-ntfy/build-secrets", { "updatesRequestHeaders": { "Authorization": "Bearer ${MY_UPDATE_TOKEN}" } }]
```

Each value is a template, and each `${NAME}` in it is filled from that environment variable at prebuild. A variable that is unset or empty fails the prebuild, and so does a template that names no variable (a value that is no secret belongs in `updates.requestHeaders`). Declare the same header in `updates.requestHeaders` with a stand-in value, so the app config says the header exists. The manifest's value replaces the stand-in and keeps the other headers declared there. It is written after expo-updates' own step, so the order of the plugins does not matter. The manifest then holds the token, so it is written with mode 0600 and the generated `android` folder must stay out of version control. Every update then launches with the same headers as the embedded one, and an app that set an override before should clear it with `Updates.setUpdateRequestHeadersOverride(null)`. Android only.

## Forwarding other apps' notifications

A second config plugin, `@drkostas/expo-ntfy/notification-relay`, adds an Android notification listener that sends each new notification of the apps you name to an HTTPS endpoint as JSON. An assistant can then read messages from apps that offer no API, as they arrive, while the phone is locked and asleep.

```json
{
  "expo": {
    "plugins": [
      ["@drkostas/expo-ntfy/notification-relay", { "packages": ["com.example.chat"] }]
    ]
  }
}
```

- The endpoint comes from `EXPO_PUBLIC_NOTIFICATION_RELAY_URL` (or the `url` option) and must be https, never the build machine. A token from `EXPO_PUBLIC_NOTIFICATION_RELAY_TOKEN` (or `token`) is sent as `Authorization: Bearer <token>`.
- Each payload has `app`, `key`, `posted_at`, `conversation`, `conversation_id`, `sender`, `text`, `is_group`, and `messages` (each `sender`, `text`, `at`) for a MessagingStyle notification.
- Only the listed packages are forwarded, never the app's own notifications and never a group summary. Each notification is sent once. What the endpoint does not take waits in a file queue of at most `queueMax` payloads (200) and is sent again with a backoff. The service logs counts, never a notification's text.
- Android asks the person to grant notification access once. Over adb it needs no taps, `adb shell cmd notification allow_listener <app package>/<package>.NotificationRelayService`.

## The glue

```ts
import { installForegroundHandler, raiseLocalNotification, useNtfy } from "@drkostas/expo-ntfy/notify";
import { defineTopicPollTask, registerTopicPoll } from "@drkostas/expo-ntfy/background";
import { pollTopicOnce } from "@drkostas/expo-ntfy";

const TOPIC = process.env.EXPO_PUBLIC_NTFY_URL;
const text = { appName: "My app", linkPrefix: "myapp://" };

installForegroundHandler();

// while a screen is open
const { connected } = useNtfy(TOPIC, () => refetch(), { text });

// while the app is closed, on a schedule the OS decides
defineTopicPollTask("topic-poll", () =>
  pollTopicOnce({ topicUrl: TOPIC!, readMark, commitMark, text,
    announce: (n) => raiseLocalNotification(n.title, n.body, n.url) }));
await registerTopicPoll("topic-poll", 15);
```

## Things I learned on real phones

- Only one part of the app may post to the tray. With the native service posting and JavaScript posting too, every message arrived twice. On Android the glue stays out of the tray by default (`TRAY_IS_NATIVE`), and keeps the in-app updates.
- Android 14 and later need the foreground service type in the `startForeground` call as well as in the manifest, or the service crashes when it starts.
- Installing an update stops the service, and nothing starts it again until someone opens the app. The receiver listens for `MY_PACKAGE_REPLACED` for that reason.
- Some Android skins (ColorOS on OPPO, for example) stop even a foreground service unless the app's battery setting allows background activity. The app cannot change that setting itself. The person has to choose "Allow background activity" in the app's battery settings.
- A device that has never watched the topic has not missed anything. On the first read the watermark takes the newest position instead of announcing the whole backlog.
- ntfy stamps whole seconds, so the watermark keeps the ids seen at its second, and no message from the same second is skipped or shown twice.
- ntfy parses `since` as a Go duration, which has no day unit. `7d` gets a 400, `24h` and `all` work.
- The background poll interval is a request. Android decides when it runs, so the poll is a floor under the socket and the service, not a replacement.

## Tests

```bash
npm ci
npm test
```

The plugin test copies a fixture Android project to a temporary folder, runs the plugin through `@expo/config-plugins`, and reads the Kotlin, Gradle and manifest files it wrote.

`npm run compile:service` compiles the service the plugin writes, against the Android 36 platform and the libraries the plugin adds. It needs Java 17 or later and an Android SDK with `platforms;android-36` (or `ANDROID_JAR` set to an `android.jar`), and downloads the Kotlin compiler and the libraries once, checked by their SHA-256 sums. `scripts/compile-service.sh FILE.kt` compiles a service that an app's prebuild wrote. CI runs it on every pull request.

## License

MIT
