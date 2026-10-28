"""RouterX's terminal — one path, hostable on its own.

Every terminal behaviour lives under this package, and nothing in the project
reaches around it: the gateway, the agent and the dashboard all import from
`terminal.*` (see `routers/admin_terminal.py`, `main.py`, `nova/agent.py`).
That is what makes the terminal separable — the whole feature is this
directory, so it can be deployed on its own machine while the rest of the app
stays connected to it.

    terminal/pty.py      real PTY session manager (the shells behind the tabs)
    terminal/sandbox.py  simulated allowlist executor for one-shot commands
    terminal/link.py     LocalLink (in-process) / RemoteLink (separately hosted)
    terminal/api.py      the HTTP contract, mounted by BOTH hosts
    terminal/service.py  the standalone host: `python3 -m terminal.service`
    terminal/config.py   the NOVA_TERMINAL_* knobs that choose the link
    terminal/run.sh      launcher for the standalone host

Dependency direction is one-way: `terminal` may use `nova`'s pure config,
model and telemetry helpers, never the other way round. `__init__` re-exports
the link seam only — it deliberately does not import `api` (FastAPI) or
`sandbox` (SQLAlchemy), so importing the seam stays cheap on both hosts.
"""
from __future__ import annotations

from .link import (
    LinkUnavailable,
    LocalLink,
    PTYUnsupported,
    RemoteLink,
    SessionGone,
    SessionLimitReached,
    SessionStartFailed,
    SessionWriteFailed,
    TerminalError,
    current,
    is_remote,
    pin,
    shell_hint_for,
)

__all__ = [
    "LinkUnavailable",
    "LocalLink",
    "PTYUnsupported",
    "RemoteLink",
    "SessionGone",
    "SessionLimitReached",
    "SessionStartFailed",
    "SessionWriteFailed",
    "TerminalError",
    "current",
    "is_remote",
    "pin",
    "shell_hint_for",
]