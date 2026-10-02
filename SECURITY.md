# Security

This project captures the screen and types passwords through a virtual keyboard that macOS treats as real hardware. Both are sensitive, so please read this before you use it.

## Reporting a problem

Please report a security problem privately through GitHub's "Report a vulnerability" button on this repository, not in a public issue. I will answer there.

## Typing a password

- Use `claude-human unlock` and `approve` only on your own Mac, with your own password, or with the clear consent of the person whose Mac and password it is. Typing someone else's password into their Mac without their consent is not what this is for.
- Give each use its own approval. A program that stores a login password and types it whenever it decides to is a program that logs in as you without asking. If you build something on top of this, have a person approve each time it types.
- The password is read from stdin and passed to the helper on stdin. It is never put in argv, the environment or a log, and this package does not store it. Whatever gives the password to the command (a script, an app, a network request) has to protect it the same way.
- The helper runs as root (`sudo -n`), because the Karabiner daemon's socket is root only. A sudo rule that lets anyone run the helper lets them type anything into any app, so limit the rule to your user and to the helper's exact path, and keep the helper's file owned by root or by you only.
- `unlock` stops after two tries and `approve` after one, because wrong passwords make macOS add a lockout delay.

## Screen capture

- Screen Recording lets a program see everything on the screen, including other people's messages and passwords shown in plain text. Grant it only to `sckshot.app` and to the process that needs it.
- If you send frames to another device, send them over an encrypted connection and only to people allowed to see the whole screen. When one window is asked for and cannot be cropped, this package returns nothing rather than the whole screen, and code built on it should keep that rule.

## Grants macOS needs

- Screen Recording (System Settings > Privacy & Security > Screen Recording) for `sckshot.app`, and for the Python process that lists windows or uses the fallback capture.
- The Karabiner-Elements driver extension, allowed in System Settings, for the virtual keyboard.
- Full Keyboard Access (System Settings > Keyboard) for `approve`, so that Tab reaches the panel's buttons.

Do not script the System Settings consent panes or edit the TCC database to grant these. Those prompts exist so that a person decides.

## Notifications

The ntfy topic URL is built into the Android app. Anyone who knows a topic on a server without access control can read it and post to it, so use a server with authentication or keep it on a private network. The plugin follows a message's link only when it points into your own app.
