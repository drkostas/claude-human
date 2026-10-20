# @drkostas/expo-adb-keeper

An Expo config plugin that turns an Android phone's wireless debugging back on after a restart, so a
machine that reaches the phone over a VPN can bring adb back without a cable.

## Why

`adb tcpip 5555` lets a phone listen for adb on every network it joins, until it restarts. After a
restart only adbd itself can listen on that port again. A machine that is not on the phone's cable
can then reach adbd only through wireless debugging, and Android switches wireless debugging off at
every restart (Wi-Fi connects after adbd starts, which Android reads as a change of network).

This plugin adds a small job to the app. After a restart, after an update of the app, and every 15
minutes on an unmetered network, it turns wireless debugging on when:

- the app holds `WRITE_SECURE_SETTINGS` (granted once over adb, below),
- USB debugging is on in Developer options,
- wireless debugging is off and nothing listens on `127.0.0.1:5555`,
- the phone is on Wi-Fi, and the job did not try on this Wi-Fi in the last hour.

The machine then finds the wireless-debugging port and runs `adb tcpip 5555` from there.
`claude-ops adb-reach <address>` from [claude-ops](https://github.com/drkostas/claude-ops) does that.

On a Wi-Fi network the phone does not trust yet, Android shows its own prompt ("Allow wireless
debugging on this network?") and keeps the setting off until the person allows it. The job never
presses that prompt, and it asks at most once an hour on one network. On mobile data alone nothing
can turn wireless debugging on, because Android allows it only on Wi-Fi.

## Use

```json
{ "expo": { "android": { "package": "org.example.myapp" },
  "plugins": [["@drkostas/expo-adb-keeper", { "fixedPort": 5555 }]] } }
```

```bash
npx expo prebuild --platform android --clean
adb install -r app-release.apk
adb shell pm grant org.example.myapp android.permission.WRITE_SECURE_SETTINGS   # once; kept across updates
adb logcat -s AdbKeeper                                                        # what each run did
```

Options: `fixedPort` (5555), `intervalMinutes` (15, the shortest period Android allows),
`retryMinutes` (60), `jobId` (41550, and the next id), `logTag` ("AdbKeeper"), `package`
(`expo.android.package`).

The permission lets the app change system settings. The job changes only `adb_wifi_enabled`, and only
under the conditions above.

## Tests

```bash
npm ci && npm test        # the plugin against a small Android project
npm run compile:kotlin    # compiles the Kotlin it writes against android.jar
```
