# Tasks, an example Expo app

A small Expo app that shows the tasks a claude-human task server has for a person. It uses `@drkostas/claude-human-client` for the task routes and `@drkostas/expo-ntfy` for notifications. It has plain React Native components and no styling library.

It has three screens.

- Waiting, the open tasks with how long each has waited.
- A task page with why the person is asked, what to do, the handoff chain in the server's order (the head marked "start here", the steps last), and the "I've done it, check" button, which asks the server to run the check and shows what it saw.
- History, what happened, newest first.

A new message on the ntfy topic refreshes the list while the app is open, and a tap on a notification with an `expotasks://task/<id>` link opens that task.

## Configure

Copy `.env.example` to `.env` and write the values in it.

- `EXPO_PUBLIC_TASKS_URL`, the task server. `claude-human task serve` listens on 127.0.0.1 only, so put something with TLS and access control in front of it and give the app that address.
- `EXPO_PUBLIC_TASKS_TOKEN`, the server's token.
- `EXPO_PUBLIC_NTFY_URL`, the topic the server announces new tasks on (`claude-human task open --ntfy-url`, or `CLAUDE_HUMAN_NTFY_URL`). The Android build stops when it is missing or a loopback address.

Open tasks with a link back to the app so a tap lands on the task.

```bash
CLAUDE_HUMAN_TASK_LINK='expotasks://task/{id}' claude-human task open approve app://backup \
    --reason "The backup needs Full Disk Access." --steps "Open System Settings and allow Backup." \
    --ntfy-url https://ntfy.example.org/tasks -- /usr/local/bin/backup --check-access
```

## Run

```bash
npm install
npm run typecheck
npm test
npx expo run:android
```

`npm test` runs the pure logic in `src/logic.ts` (links, the task page, the answers to show) under vitest. The two packages are `file:` dependencies on this repository, so `metro.config.js` watches their real folders and `tsconfig.json` sets `preserveSymlinks`, which makes their imports resolve from this app's `node_modules`.
