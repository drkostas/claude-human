"""Hand a person one window of this Mac on their phone, and let them drive it.

The station is a small HTTP server. It streams one window (or the display) as MJPEG and turns the
person's taps and keys into clicks and key presses on the Mac. MJPEG is one multipart response that
any WebView can read with no WebRTC and no client library.

Who may use it is decided by a ``StationAuth`` that the caller passes in. ``TokenAuth`` is the
simple one: a single token from a file or the environment. A larger system can grant one window per
task and revoke the grant when the task ends, by implementing ``check`` and ``app_for``, and can
record what happened through ``perform`` and ``on_input``.

``prepare`` puts a window into the right state before the person is handed it.

    from claude_human.station import TokenAuth, make_server
    srv = make_server(TokenAuth(token_file="~/.config/claude-human/station-token"), port=8789)
    srv.serve_forever()
"""
from __future__ import annotations

from .auth import (ENV_TOKEN, ENV_TOKEN_FILE, Grant, StationAuth, TokenAuth, Unavailable,
                   default_token_file, load_or_create_token)
from .input import QuartzInput
from .prepare import Preparer
from .server import DEFAULT_PORT, Handler, Station, StationServer, make_server
from .view import render as render_view

__all__ = [
    "DEFAULT_PORT", "ENV_TOKEN", "ENV_TOKEN_FILE", "Grant", "Handler", "Preparer", "QuartzInput",
    "Station", "StationAuth", "StationServer", "TokenAuth", "Unavailable", "default_token_file",
    "load_or_create_token", "make_server", "render_view",
]
