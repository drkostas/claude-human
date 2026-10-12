# Contributing

Issues and pull requests are welcome. macOS changes how capture, the lock screen and password panels behave from one version to the next, so reports of a macOS version where something stopped working are very useful. Please include the macOS version, the output of `claude-human state`, and the exact message the command printed.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test,macos]'
.venv/bin/pytest
cd js && npm ci && npm test
```

Please add a test for every change in behaviour. The tests must run without any grant and without Karabiner, so the typing logic is tested with a fake helper and the detection with recorded window lists. Please never add a test that types a real password or captures a real screen.

For the Android plugin, please say which phone and Android version you tried it on, because some Android skins stop background services in their own ways.

## Writing for this repository

Everything here is public, including commit messages and pull request descriptions. Please write them for a user of the package. Do not name other projects, people or machines that use it, and keep real hostnames, IP addresses, paths and credentials out of code, tests and history (examples use `example.org` and `192.0.2.x`). A check on every pull request looks for terms that must not appear here.
