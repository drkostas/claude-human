"""Who may use the station, and what the station tells the caller when something happens.

The server never decides on its own who a request comes from. It reads the bearer token from the
``Authorization`` header (and from nowhere else) and asks a ``StationAuth`` what that token may do.
The answer is a ``Grant`` or None.

A grant is one of two kinds.

- A master grant (``master=True``) may list every window, stream any window, and choose the window
  each request is for. This is the owner's own tooling.
- A pinned grant (``master=False``) may drive one station. The server asks ``app_for`` which
  application that station is, and every frame and every input of that request goes to that
  application's window, whatever the request asks for. When ``app_for`` answers None the station is
  the whole display, which is a deliberate answer and not a fallback.

``app_for`` must raise ``Unavailable`` when it cannot answer (a database that does not respond, for
example). The server then refuses the request. Returning None there would turn a passing error into
access to the whole screen.

The two hooks ``perform`` and ``on_input`` let the caller record what happens. The default class does
nothing in either.
"""
from __future__ import annotations

import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ENV_TOKEN = "CLAUDE_HUMAN_STATION_TOKEN"
ENV_TOKEN_FILE = "CLAUDE_HUMAN_STATION_TOKEN_FILE"


def default_token_file() -> Path:
    return Path.home() / ".config" / "claude-human" / "station-token"


class Unavailable(Exception):
    """The auth could not be asked. This is not the same answer as "no"."""


@dataclass(frozen=True)
class Grant:
    """What one bearer token may do, for the length of one request."""
    master: bool
    station: Optional[str] = None
    holder: Optional[str] = None


class StationAuth:
    """The interface the server calls. Subclass it and override what you need.

    ``check`` is called on every request, so a grant that is revoked stops working at once. Do not
    cache its answer for longer than a request."""

    def check(self, secret: str) -> Optional[Grant]:
        """The grant this secret carries, or None. Never raise for a secret you do not know."""
        return None

    def app_for(self, grant: Grant) -> Optional[str]:
        """The application a pinned grant may drive, or None for the whole display.

        Raise ``Unavailable`` when the answer cannot be found."""
        return None

    def perform(self, grant: Grant, act: str, run: Callable[[], dict]) -> dict:
        """A named act that reaches past one window (``spotlight`` opens Spotlight on the whole Mac).

        ``run`` presses the keys and returns the input result. Override this to record who did it,
        or to refuse it. The default runs it."""
        return run()

    def on_input(self, grant: Grant, msg: dict, result: dict) -> None:
        """Called after every input the station handled, with the message and its result."""


class TokenAuth(StationAuth):
    """One shared token, for a person who runs the station for themselves.

    The token is compared in constant time. It comes from the ``token`` argument, or else is read
    from ``token_file`` at the moment of use, so a token written again takes effect without a
    restart. With ``app`` set, the token is a pinned grant for that application and cannot list or
    choose windows. Without it, the token is a master grant."""

    def __init__(self, token: Optional[str] = None, *, token_file: Optional[os.PathLike] = None,
                 app: Optional[str] = None):
        if not token and not token_file:
            raise ValueError("TokenAuth needs a token or a token file")
        self._token = token
        self._file = Path(token_file).expanduser() if token_file else None
        self.app = app

    def token(self) -> str:
        if self._token:
            return self._token
        try:
            return self._file.read_text().strip()
        except OSError:
            return ""

    def check(self, secret: str) -> Optional[Grant]:
        want = self.token()
        if not secret or not want or not hmac.compare_digest(secret.encode(), want.encode()):
            return None
        if self.app:
            return Grant(master=False, station=f"app:{self.app}")
        return Grant(master=True)

    def app_for(self, grant: Grant) -> Optional[str]:
        return self.app


def load_or_create_token(path: Optional[os.PathLike] = None, *,
                         prefix: str = "station_") -> tuple[str, Path]:
    """The token from ``path`` (or the station's default file), made once with mode 0600 if missing.

    Returns the token and the file it lives in. The token is never printed by this package."""
    p = Path(path).expanduser() if path else default_token_file()
    if p.is_file():
        tok = p.read_text().strip()
        if tok:
            return tok, p
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = prefix + secrets.token_urlsafe(24)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return tok, p
