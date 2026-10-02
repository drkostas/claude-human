# @drkostas/expo-ntfy

This package gets messages from a self-hosted [ntfy](https://ntfy.sh) server to an Android phone running an Expo app, without Firebase. I wrote it because the JavaScript socket in my app stopped receiving the moment the phone locked, so notifications only reached a phone that someone was already looking at.

It has three parts.

- A config plugin that adds a native Android foreground service. The service holds the ntfy WebSocket while the app is closed and posts every message to the tray itself.
- The wire logic (`proto` and `poll`), with no Expo or React Native imports, so it runs in Node and is tested there.
- The Expo glue (`notify` and `background`) for permissions, local notifications, the socket while the app is open, taps, and a background poll.

## Install

```bash
npm install @drkostas/expo-ntfy
```

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
| `defaultTitle` | `expo.name` |
| `linkPrefix` | `<expo.scheme>://`, or no links when there is no scheme |
| `alertChannelId`, `alertChannelName`, `alertChannelDescription` | `ntfy_messages`, `expo.name`, a short text |
| `ongoingChannelId`, `ongoingChannelName`, `ongoingChannelDescription` | `ntfy_connection`, `Connection`, a short text |
| `ongoingTitle`, `ongoingText` | `<expo.name> is connected`, `waiting for messages` |
| `logTag` | `NtfyService` |

What it writes at prebuild.

- `NtfyService.kt` and `BootReceiver` next to `MainActivity`, in the app's package.
- The URL as a BuildConfig field. It is replaced on every build, so building again with a different URL corrects a wrong one.
- A call in `MainActivity.onCreate` that starts the service.
- The manifest entries for the service (type `dataSync`), the receiver, and the permissions `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, `RECEIVE_BOOT_COMPLETED` and `WAKE_LOCK`.

A message's click URL opens the app only when it starts with `linkPrefix`. A notification is data from the network, so any other link is shown in the text but never followed.

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

## License

MIT
