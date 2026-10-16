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

### A server that requires authentication

An ntfy server started with `--auth-default-access deny-all` answers a subscriber without credentials with 401. Give the app an access token (`ntfy token add <user>` on the server) through `EXPO_PUBLIC_NTFY_TOKEN` at prebuild time, or the `token` option. The service then sends `Authorization: Bearer <token>` when it opens the socket, never the token in the URL. `pollTopicOnce` and `useNtfy` take the same token as their `token` option, so the background poll and the foreground socket authenticate too.

The token is part of the app you ship, so anyone holding the APK can read it. Give the app a user that may only read its topics (`ntfy access <user> <topic> read-only`), and publish with a different token.

A message's click URL opens the app only when it starts with `linkPrefix`. A notification is data from the network, so any other link is shown in the text but never followed.

## What a message shows

- Folded, on the lock screen and in the shade, a notification shows the message's title and the first line of its text. Pulled down, it shows the whole text and its buttons.
- Each `view` action becomes a button, up to three, in the order they came. A button opens its URL in the app the same way a tap opens `click`, and only when the URL starts with `linkPrefix`. `http` and `broadcast` actions are not drawn. A button does not dismiss the notification, so ntfy's `clear` has no effect. Dismissing it first would need a receiver between the tap and the app, and Android 12 and later do not let such a receiver open an activity.
- The priority picks one of three channels. At `alarmPriority` or above (5 by default) the message rings the alarm channel, which plays the phone's alarm sound as an alarm. At `chimePriority` or above (4) it chimes on the alert channel, which keeps the id messages were posted on before there were three channels. Below that it is silent, in the tray with no sound. ntfy's default priority is 3, so set `chimePriority` to 3 if a message published without a priority should still make a sound.
- Do Not Disturb lets the alarm through wherever it lets alarms through. The alarm channel also asks to bypass Do Not Disturb, and Android keeps that only when the app has Do Not Disturb access, which the person grants in the system settings. The app is listed there because the manifest asks for `ACCESS_NOTIFICATION_POLICY`.
- With `subTextTagPrefix` set (for example `"area:"`), the first tag that starts with it is shown, without the prefix, next to the app's name in the notification's header.
- Android fixes a channel's sound when the channel is created. The person can still change each channel's sound in the app's notification settings.

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
