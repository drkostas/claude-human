# claude-human

claude-human is a small set of tools for a Mac that a person and an agent share. I run a system on my own Mac that hands tasks to me on my phone when it needs a person (approve a password prompt, look at a window, type a password at the lock screen). These are the parts of it that are not tied to that system.

There are three pieces.

- `claude_human.screenshot` captures the screen or one window through ScreenCaptureKit, lists the windows a person works in, and tells a locked screen apart from a password panel.
- `claude_human.unlock` types a password at the lock screen or into a SecurityAgent password panel through Karabiner's virtual HID keyboard, and then checks that it worked.
- `@drkostas/expo-ntfy` (in `js/`) gets messages from a self-hosted ntfy server to an Android phone without Firebase, through a native foreground service that an Expo config plugin adds to the app.

The Python parts need macOS. The JavaScript part needs an Expo app.

## Install

```bash
pip install 'claude-human[macos]'
claude-human build-tools
```

`build-tools` compiles the two helpers into `~/.local/share/claude-human/bin` (change it with `--out` or `CLAUDE_HUMAN_BIN_DIR`). It needs the Xcode command line tools and git. The lock screen helper also needs [Karabiner-Elements](https://karabiner-elements.pqrs.org/) installed, because it talks to Karabiner's virtual HID daemon.

```bash
claude-human build-tools --only sckshot --bundle-id org.example.sckshot --identity "Apple Development: Your Name (TEAMID)"
claude-human build-tools --only vhid --pqrs-commit <commit>
```

The signing identity defaults to the first "Apple Development" identity in your keychain, and to an ad-hoc signature when there is none. With an ad-hoc signature the Screen Recording grant does not survive a rebuild, because macOS ties it to the signature. Keep the bundle id the same between builds for the same reason. `--identity none` skips signing (CI uses it).

## Screenshots

```bash
claude-human windows                                   # the ordinary windows on screen, as JSON
claude-human screenshot --out screen.png               # the main display
claude-human screenshot --out safari.jpg --app Safari --max-width 1200
claude-human state                                     # locked? password panel up? a system dialog holding focus?
```

```python
from claude_human import screenshot

wid = screenshot.resolve("Safari", None)               # look the window up again on every request
jpeg, width, height = screenshot.frame(wid, max_width=1100, quality=0.55)
```

A few things I learned while building this.

- On recent macOS the older capture call (CGWindowListCreateImage) still works but is throttled to about one frame every 30 seconds. ScreenCaptureKit is not throttled, but its async completion never fires through PyObjC, so `sckshot` is a small Swift tool that the Python side runs. The old call is kept as a fallback.
- A window id changes every time its application restarts. Store the application name and look the id up again (`resolve`), or a stored id can reach a different window.
- When one window is asked for and the fallback cannot find its rectangle (for example, it is on another desktop), `capture` returns nothing. It never returns the whole screen in its place, because that would show every other window to someone who was given one.
- `frame` returns nothing at once while the session is locked. macOS refuses the capture then, and the fallback would block for 30 seconds and return black.
- A SecurityAgent password panel sets the same "locked" bit as the lock screen, while the desktop behind it is live. `auth_prompt()` finds the panel (a small SecurityAgent window, between the size of its helper windows and the size of the lock screen), and `frame` still captures when one is up.
- Without the Screen Recording grant, macOS lists windows with empty titles. Empty titles are the sign that the grant is missing.

## Typing at the lock screen and into password panels

The lock screen and SecurityAgent panels accept input only from real hardware. Synthetic key events, synthetic clicks and Screen Sharing input are all dropped there. Karabiner's DriverKit virtual HID keyboard is seen by macOS as a physical keyboard, so `vhid_type` can type where nothing else can.

```bash
claude-human unlock < password.txt       # type at the lock screen, then wait for the lock to clear
claude-human relock                      # sleep the display, then wait for the lock to set
claude-human use-password                # a Touch ID panel: press Return to switch to the password field
claude-human approve < password.txt      # type into the password panel, then wait for it to close
```

```python
from claude_human import unlock

ok, detail = unlock.unlock(password)                       # (True, "Unlocked.")
ok, detail = unlock.approve(password, prompt_present=my_detector)
```

How it behaves.

- The password goes to the helper on stdin. There is no command line option for it, and it never goes into argv, the environment or a log. When stdin is a terminal, the command asks for it without echo.
- Nothing is typed unless the target is there. `unlock` returns at once when the Mac is not locked, and `approve` refuses when no password panel is on screen.
- The result is checked, not assumed. `unlock` watches the lock bit clear, `approve` watches the panel close, and `relock` watches the lock bit set.
- The first keys at a lock screen are often lost because the field has not taken focus yet, so `unlock` tries a second time. It stops after two tries, because each try is a real password entry and macOS adds a delay after several wrong ones.
- The helper clears the field first (Cmd+A, Backspace) so leftover text cannot join the password.
- On the macOS password panel, Return does not press OK, and a synthetic click never reaches it. Space presses the focused button, so `approve` types the password, Tab, Tab and Space. This needs Full Keyboard Access to be on (System Settings > Keyboard), so that Tab reaches buttons.
- The helper runs under `sudo -n`, because the Karabiner daemon's socket is only open to root. Give your user a sudo rule for the helper's path, or run it as root.

Please read [SECURITY.md](SECURITY.md) before you use this part. It types a password, and it should only ever do that on your own Mac.

## Phone notifications without Firebase

`js/` is the npm package `@drkostas/expo-ntfy`. It has a config plugin, the ntfy wire logic, and the Expo glue for notifications. See [js/README.md](js/README.md).

## What macOS needs

- Screen Recording for `sckshot.app` (fast capture), and for the process that lists windows (window titles and the fallback capture). macOS judges a process started by launchd by the binary that runs, so grant it to that interpreter and not only to your terminal.
- Karabiner-Elements, with its driver extension allowed, for the virtual keyboard.
- Full Keyboard Access for `approve`.

A major macOS upgrade can reset these grants. Nothing can grant them again except a person in System Settings, which is how it should be.

## Claude Code skill

The package ships a Claude Code skill that teaches an agent how to use all of this safely. It covers building and signing the helpers, the macOS grants and what resets them, the consent rules for typing a password, the Android delivery traps, and a catalogue of the failures behind each rule.

```bash
claude-human skill                      # writes ~/.claude/skills/claude-human/SKILL.md
claude-human skill --dir ./skills       # another skills folder
```

The same file is at [skill/SKILL.md](skill/SKILL.md) for anyone who uses only the npm package.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test,macos]'
.venv/bin/pytest
cd js && npm ci && npm test
```

The tests do not need any grant. They cover the argument parsing, the path settings, the window and panel detection on recorded window lists, the typing logic with a fake helper, the ntfy logic, and the config plugin run against a fixture Android project. On macOS one test also compiles `sckshot` without signing it.

## License

MIT
