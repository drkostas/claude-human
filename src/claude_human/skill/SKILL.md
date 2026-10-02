---
name: claude-human
description: Use when an assistant has to act on a person's Mac or reach that person on an Android phone, through the claude-human package and @drkostas/expo-ntfy. Covers building and signing the sckshot and vhid_type helpers, the macOS grants each needs and what resets them, capturing the screen or one window, typing a password at the lock screen or into a SecurityAgent password panel with the person's consent, locking the Mac again afterwards, and delivering ntfy notifications to an Android phone that the OS does not stop. Also use it when a capture is black, white or slow, when a window list has empty titles, when typed keys do nothing, or when phone notifications stop arriving.
---

# claude-human

This skill is for an assistant that works on a Mac a person also uses, and that needs the person now and then. The Python package `claude_human` captures the screen and types where macOS accepts only hardware keys. The npm package `@drkostas/expo-ntfy` brings messages to an Android phone without Firebase. Every rule below came from a failure on a real Mac or a real phone, and the failure catalogue at the end says how each one was caught.

## Rules that come before any command

- Type a password only on the person's own Mac, only for a task the person asked for, and only with an approval for that one use. A stored password that a program types whenever it decides is a program that logs in as the person without asking. Send each request to the person's phone and type only after they approve it.
- The password goes on stdin. Read it from a secret store or from the person's answer and pipe it in. Never pass it as an argument, an environment variable or a log line. The CLI has no option that takes a password, on purpose.
- Observe the result. A command that exits 0 has typed keys, which is not the same as a cleared lock or a closed panel. The functions in `claude_human.unlock` watch the lock bit or the panel, and report failure when it does not change.
- Lock the Mac again with `claude-human relock` when you were the one who opened the lock, and only then.
- Before opening the lock at all, ask whether the work needs the screen. The lock blocks keys and clicks on the console. It does not block a shell process, so command line or network work can run while the Mac stays locked.
- A person grants every macOS permission. Never press a consent pane with synthetic input, never write the TCC database, never answer a consent dialog for the person. Open the pane, tell the person what to choose, and check the result yourself.
- On the phone, wake the screen freely, but never type a PIN or pass the fingerprint step. Those stay with the person.

## Building the helpers

```bash
pip install 'claude-human[macos]'
claude-human build-tools                                   # both helpers into ~/.local/share/claude-human/bin
claude-human build-tools --only sckshot --bundle-id org.example.sckshot --identity "Apple Development: Name (TEAMID)"
claude-human build-tools --only vhid --pqrs-commit <commit>
security find-identity -v -p codesigning                   # which signing identities exist
```

- `build-tools` needs the Xcode command line tools and git. The output folder is `--out`, or `CLAUDE_HUMAN_BIN_DIR`. The CLI finds the helpers there, or through `--sckshot` and `--vhid`, or `CLAUDE_HUMAN_SCKSHOT` and `CLAUDE_HUMAN_VHID`.
- `sckshot` is built as `sckshot.app`, not a bare binary. The Screen Recording list in System Settings accepts only app bundles, so a bare binary can never hold the grant, and ScreenCaptureKit then waits for ever without a prompt.
- Sign `sckshot.app` with a real "Apple Development" identity and keep the bundle id (`--bundle-id`, `CLAUDE_HUMAN_BUNDLE_ID`) the same across builds. macOS ties the grant to the signature. An ad-hoc signature changes on every build, so each rebuild silently loses the grant. `--identity -` signs ad-hoc, `--identity none` skips signing (CI only), and the default `auto` takes the first Apple Development identity and falls back to ad-hoc with a warning.
- `--usage-text` (`CLAUDE_HUMAN_USAGE`) is the text macOS shows when it asks for Screen Recording.
- `vhid_type` speaks to the Karabiner-VirtualHIDDevice daemon, which ships with Karabiner-Elements. It must be compiled against the pqrs client library at the same protocol version as the installed daemon. The default commit matches Karabiner-Elements 15.4.0. After a Karabiner upgrade, find the Karabiner-DriverKit-VirtualHIDDevice submodule commit of the new release and pass it with `--pqrs-commit` (or `CLAUDE_HUMAN_PQRS_COMMIT`). `CLAUDE_HUMAN_PQRS_SRC` names an existing checkout to build from instead of a fresh clone.
- `vhid_type` runs under `sudo -n`, because the daemon's socket is open only to root. Give the user a sudoers rule for the helper's exact path and nothing wider, keep the file owned by root or by the user, and validate the rule set before you rely on it.

```bash
echo "$USER ALL=(root) NOPASSWD: $HOME/.local/share/claude-human/bin/vhid_type" | sudo tee /etc/sudoers.d/vhid_type
sudo chmod 440 /etc/sudoers.d/vhid_type && sudo visudo -c
sudo -n -l "$HOME/.local/share/claude-human/bin/vhid_type"   # prints the path when the rule works, types nothing
```

A sudoers file with mode 0644 is ignored by sudo without any message, so the `chmod 440` matters.

## The macOS grants, and how to check each

| What | Needed by | How to check |
|---|---|---|
| Screen Recording for `sckshot.app` | fast capture | `claude-human screenshot --out /tmp/t.png` prints a size, and the image is not black |
| Screen Recording for the process that lists windows | window titles, the fallback capture | `claude-human windows` shows titles, not empty strings |
| Karabiner driver extension allowed | `vhid_type` | `systemextensionsctl list` shows the Karabiner driver as activated and enabled |
| Full Keyboard Access | `approve` | `defaults read -g AppleKeyboardUIMode` prints 2 or more |
| Automation of another app | any agent that scripts that app | the first call raises a dialog for the person |

- macOS judges a process started by launchd by the binary that runs it, not by the terminal you tested from. The same script captures from a granted terminal and returns nothing from a launch agent. Grant the interpreter or app the agent runs.
- A child process detached from a granted terminal keeps the terminal's grant, so it works until the next reboot and then fails. Treat that as a test, not as a deployment.
- Empty window titles are the reliable sign of a missing Screen Recording grant. macOS redacts titles for a process without it.
- A major macOS upgrade can reset every Screen Recording and Accessibility grant and remove entries from the list. After an upgrade, run the checks above before trusting anything.
- To send the person to the right pane, run `open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"`. The older `com.apple.preference.security?Privacy_ScreenCapture` form opens nothing on recent macOS and reports no error, so confirm which app is in front afterwards.
- A new launch agent that scripts another app (a terminal, for example) raises an Automation consent dialog on its first run. Until the person answers it, every script call to that app that lists windows hangs, from every process. A call that only asks for the app's version still answers, which makes the app look healthy.
- A Mac upgrade can also disable Screen Sharing. That matters only for the person's own remote viewing, since Screen Sharing input never reaches the lock screen (see the catalogue).

## Screenshots

```bash
claude-human windows                                        # JSON list of ordinary windows
claude-human screenshot --out shot.png                      # the main display
claude-human screenshot --out app.jpg --app Safari --max-width 1200
claude-human state                                          # {"locked", "auth_prompt", "blocked_by"}
```

```python
from claude_human import screenshot
wid = screenshot.resolve("Safari", None)       # look the id up again on every request
shot = screenshot.frame(wid, max_width=1100)   # (jpeg, width, height), or None
```

- Store an application name, never a window id. A `CGWindowID` changes whenever the app restarts, and a stored id can later reach a different window.
- Use `frame` rather than `capture` in anything that runs unattended. While the session is locked macOS refuses the capture, and the old capture call blocks about 30 seconds and returns black. `frame` returns None at once instead.
- A SecurityAgent password panel sets the same lock bit as the lock screen, while the desktop behind it is live. `auth_prompt()` finds the panel by owner and size (larger than its tiny helper windows, smaller than 70 percent of the display), and `frame` still captures while one is up. The panel is its own window, so a viewer that shows only the task's window never shows it. Poll `auth_prompt()` separately.
- When one window is asked for and its rectangle cannot be found (another desktop, for example), the result is None. Never fall back to the whole screen, because that shows every other window to someone who was given one.
- `blocked_by()` names a system dialog that holds focus. While one is up, synthetic clicks are accepted and delivered nowhere, so stop and look before clicking again.
- Read the image you captured. A file of the right size can still be black or white.

## Typing at the lock screen and into password panels

The lock screen and SecurityAgent panels take input only from hardware. Synthetic key events, synthetic clicks and Screen Sharing input are all dropped there. The Karabiner virtual keyboard is seen as a physical keyboard, so `vhid_type` can type where nothing else can.

```bash
claude-human state                                              # look first
security find-generic-password -s mac-login -w | claude-human unlock   # keychain item named mac-login
claude-human use-password                                       # a Touch ID panel, show its password field
security find-generic-password -s mac-login -w | claude-human approve
claude-human relock                                             # when you opened the lock
```

```python
from claude_human import unlock
ok, detail = unlock.unlock(password)              # (True, "Unlocked.") or (False, reason)
ok, detail = unlock.approve(password)             # refuses with unlock.NO_PROMPT when no panel is up
ok, detail = unlock.relock()                      # watches the lock bit set
```

- `unlock` returns at once when the Mac is not locked, so it never types a password into a live desktop. It wakes the display, taps Shift to show the field, clears the field (Cmd+A, Backspace), types, presses Return, then watches the lock bit. The first keys are often lost while the field takes focus, so it tries a second time. `--attempts` is capped at 2, because every try is a real password entry and wrong entries add a lockout delay.
- The clear step exists because leftover text in the field joined the password and made it wrong.
- On the LocalAuthentication panel, Return does not press OK. Return presses the default button, Space presses the focused button, and the tab order is field, Cancel, OK. So `approve` types the password, Tab, Tab, Space in one burst. That needs Full Keyboard Access, or Tab never reaches the buttons.
- A Touch ID panel opens without a password field. Its default button is "Use Password...", so `use-password` presses Return only, and types nothing.
- `approve` reports success only when the panel closes. A wrong password leaves it open, which is a failure.
- Check the effect of the task in its own source of truth, not in the settings window. A system extension that was approved for removal stays loaded until a reboot, and its toggle in System Settings keeps showing it as on. `systemextensionsctl list` showed the real state.
- `relock` sleeps the display with `pmset displaysleepnow` and watches the lock bit. It locks only when the Mac requires a password right after sleep, which is the Mac's setting, so a failure there is reported, not assumed away.
- Keep the Mac from locking during GUI work. If it locks again mid task, every synthetic click and scroll goes to the lock screen and does nothing, with no error.
- Window managers and placement tools can still list windows on a locked console, but cannot act on them. Clear the lock before placing a window.

- Screen Sharing is not a way past the lock. Its high performance mode drives a separate virtual display, never the console. Its classic mode with the dedicated Screen Sharing password shows a live login window that drops every key it is sent, by design, so that this password cannot be used to guess the login password. Signing in to Screen Sharing with the account name and the account password does attach to that account's session, which is the person's own remote route, not the assistant's.
- To check that the virtual keyboard reaches the system at all without typing a password, read `HIDIdleTime` with `ioreg -c IOHIDSystem` before and after a harmless key. It falls back near zero only when a hardware-class event arrived.

## A GUI task from start to end

1. Ask whether the task needs the screen. A command, an API or a network call usually does the same job while the Mac stays locked.
2. Run `claude-human state`. Note whether the Mac is locked and whether a panel or a system dialog is up.
3. If it is locked, send the person a request for this one use, wait for the approval, then pipe the password into `claude-human unlock` and read its `OK` or `FAIL` line.
4. Keep the Mac awake for the duration (`caffeinate`), and open the task's window where it does not cover the window the person was using.
5. Before each click, capture the window with `screenshot.frame` and read the image. After each click, capture again and confirm the change.
6. Poll `auth_prompt()` the whole time. When a panel appears, capture it with `frame(prompt["wid"])`, show it to the person with the approval request, run `use-password` if it shows no field, then `approve`.
7. Confirm the task's result in its own source of truth.
8. Run `claude-human relock` if you opened the lock, and read its result.

## Notifications on an Android phone

The config plugin writes a Kotlin foreground service that holds the ntfy WebSocket and posts each message to the tray, a receiver that starts it after a reboot or an app update, and the manifest entries. It runs at prebuild, because `expo prebuild --clean` regenerates `android/` and hand edits there are lost.

```json
{ "expo": { "scheme": "myapp", "android": { "package": "org.example.myapp" },
  "plugins": [["@drkostas/expo-ntfy", { "alertChannelName": "My app" }]] } }
```

```bash
EXPO_PUBLIC_NTFY_URL=https://ntfy.example.org/mytopic npx expo prebuild --platform android --clean
adb devices -l                                                    # pick the phone, not an emulator
PHONE=<serial from adb devices>; PKG=org.example.myapp
adb -s "$PHONE" install -r app-release.apk
adb -s "$PHONE" logcat | grep NtfyService                         # "socket failed" means the address is wrong
adb -s "$PHONE" shell dumpsys notification --noredact | grep "$PKG"   # did a message reach the tray
```

- A socket held in JavaScript stops seconds after the screen locks. The server's subscriber count went from 1 to 0 right after screen off. Only a foreground service keeps the connection without Firebase.
- The topic URL is fixed into the build. A phone built with `localhost` connects to itself and receives nothing, while the app looks healthy. The plugin refuses a loopback, empty or non-http(s) URL at prebuild, and replaces an existing value on every build. Build the phone app with the phone's address, never with the address a web preview on the laptop uses.
- A release build refuses plain HTTP by default. Serve ntfy over HTTPS on an address the phone can reach (a private network is fine), and keep the topic on a server with access control.
- Only one part of the app may post to the tray. With the service and JavaScript both posting, every message arrived twice. `TRAY_IS_NATIVE` keeps the JavaScript glue out of the tray on Android. Count the notification records in `dumpsys notification` to check, rather than trusting one delivery.
- Android 14 and later need the service type in the `startForeground` call as well as in the manifest, or the service crashes on start.
- Installing an update stops the app and its service, and nothing restarted it until someone opened the app. The receiver listens for `MY_PACKAGE_REPLACED` for that reason. Test by installing and not opening the app.
- Some Android skins stop even a foreground service. On ColorOS, the app's battery setting must be "Allow background activity" (the default "Smart mode" stopped the service within seconds of screen off), and "Allow auto-launch" must be on, or the boot broadcast never arrives. Neither can be changed with adb permission commands. Tell the person, or drive the settings screen yourself if the task allows it.
- ColorOS settings that adb cannot change can still be reached by reading the screen with `adb -s "$PHONE" shell uiautomator dump` and tapping the radio button at the bounds it reports, when the person has asked for the task.
- Before every tap on the phone, bring the target app to the front and confirm it is the resumed activity. Always pass `adb -s`, because an attached emulator can take a bare `adb` command and report success from the wrong device. Find a button by its resource id or exact text in the UI tree, never by a coordinate remembered from an earlier screen.
- ntfy reads `since` as a Go duration with no day unit. `7d` returns 400, `24h` and `all` work.
- A first read adopts the newest position instead of announcing the whole backlog (`newSince` with `ZERO`). ntfy stamps whole seconds, so the read mark also keeps the ids seen at its second.
- `pollTopicOnce` with `registerTopicPoll` is a floor under the service. Android decides when it runs.
- Hermes stores any string with a non-ASCII character as UTF-16 in the bundle. Search for both encodings before you decide a string is missing from a build.

## Failure catalogue

| Symptom | Cause | How it was caught | Fix |
|---|---|---|---|
| Captures take about 30 seconds each | the old capture call is throttled on recent macOS | timing each call against ScreenCaptureKit | use `sckshot`, keep the old call as fallback |
| ScreenCaptureKit never returns from Python | its async completion does not fire through PyObjC | a probe with a running run loop never saw the callback | the compiled `sckshot` helper |
| `sckshot` hangs with no prompt | a bare or ad-hoc binary cannot hold the grant | `codesign -dv` showed no team id | signed `.app` bundle, stable bundle id |
| Every capture fails, even Apple's | the capture service is wedged system wide | `screencapture -x` failed with "could not create image from display" | a reboot, the fallback serves meanwhile |
| Frames white or invalid JPEG from an agent | image encoding breaks in some launchd contexts | the same code worked from a shell | `sckshot` scales and encodes itself |
| Black frame and a long hang | capture while locked | `claude-human state` said locked | `frame`, which returns None at once |
| Stream blank while a password panel was up | the panel sets the lock bit | the panel showed in the window list | `auth_prompt()` exempts it |
| Window titles empty | no Screen Recording for that process | same script worked from a terminal | grant the agent's interpreter |
| Typed keys reached nothing at the lock screen | synthetic and Screen Sharing input are dropped there | `HIDIdleTime` in `ioreg -c IOHIDSystem` reset only for the virtual keyboard | `vhid_type` |
| `vhid_type` sends but nothing types | helper built for another daemon protocol version | the daemon version did not match the client headers | `--pqrs-commit` of the installed release |
| Password rejected after a failed try | leftover text joined the password | a test field held "GARBAGEtext" | the clear step |
| `approve` typed but the panel stayed | Return does not press OK there | a wrong password plus Tab, Space left it open, Cancel closed it | Tab, Tab, Space |
| A password check accepted any password | passwordless sudo never prompted | a deliberately wrong password returned success | never use sudo to check a password |
| Clicks and scrolls did nothing for an hour | the Mac had locked again | the lock bit, read at last | keep it awake, check `state` first |
| Phone silent once locked | JavaScript socket suspended | server subscriber count fell to 0 | foreground service |
| Phone silent for days | `localhost` baked into the build | `logcat` showed the cleartext refusal for localhost | plugin refuses loopback |
| Every message twice | two parts posted to the tray | two notification records per message | `TRAY_IS_NATIVE` |
| Silent after each update | update stops the service | install without opening, `dumpsys` showed no service | `MY_PACKAGE_REPLACED` receiver |
| Service dies on one brand of phone | skin battery policy | service gone seconds after screen off | "Allow background activity", auto-launch |
| A tap landed in the wrong app | coordinates reused without foregrounding | a screenshot after the tap | foreground and confirm before every step |
