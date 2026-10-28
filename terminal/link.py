"""Terminal link — the one seam between RouterX and its terminal.

The interactive terminal can be hosted **separately** from the rest of the
app (its own process, its own port, another machine) while the project stays
connected to it:

    NOVA_TERMINAL_URL unset → LocalLink   — `terminal.pty.manager` in this process
    NOVA_TERMINAL_URL set   → RemoteLink  — HTTP + SSE to the terminal service

Both links expose the same async surface, so the routes in
`terminal/api.py` — and therefore the dashboard, the API and the agent —
behave identically in both modes. `terminal/service.py` mounts those same
routes over a LocalLink, which is what keeps the two hosts contract-identical
instead of merely similar.

Errors cross the seam as exceptions carrying a stable `code`; the standalone
service serialises them as `{"error": …, "code": …}` and the link rebuilds
them, so a remote failure reports the same message a local one does.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from . import config          # `terminal/config.py` — this terminal's own settings
from .pty import (
    PTYUnavailable,
    SessionLimitReached,
    agent_session,
    ensure_default,
    manager,
    run_in_session,
    shell_hint,
)

log = logging.getLogger("terminal.link")

# Long-lived SSE: no read timeout, or the stream dies on an idle shell.
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=30.0)
# Everything else is a short request/response.
CALL_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=30.0)

# Where the standalone service mounts the shared routes.
REMOTE_PREFIX = "/terminal/pty"


# --------------------------------------------------------------------------- #
# Errors — the vocabulary both hosts speak
# --------------------------------------------------------------------------- #


class TerminalError(RuntimeError):
    """Any terminal failure that crosses the link. `.code` is the wire form."""

    code = "terminal_error"
    status = 500


class SessionGone(TerminalError):
    code = "session_gone"
    status = 404


# `terminal.pty.SessionLimitReached` is the single session-cap error in the
# codebase (the manager raises it, the routes catch it); it is re-exported here
# so a caller holding a link only ever imports one module.
SessionLimitReached.code = "session_limit"  # type: ignore[attr-defined]
SessionLimitReached.status = 409           # type: ignore[attr-defined]


class PTYUnsupported(TerminalError):
    """This host has no POSIX pseudo-terminal, so real shells cannot start.

    A typed 503 answer — not a crash and not an empty session — so the
    dashboard can say the truth while the sandbox command path keeps working.
    """
    code = PTYUnavailable.code
    status = PTYUnavailable.status


def _as_link_error(err: Exception) -> Exception:
    """Translate the PTY manager's own typed error into the link vocabulary."""
    if isinstance(err, PTYUnavailable):
        return PTYUnsupported(str(err))
    return err


class SessionStartFailed(TerminalError):
    code = "session_start_failed"
    status = 500


class SessionWriteFailed(TerminalError):
    code = "session_write_failed"
    status = 409


class LinkUnavailable(TerminalError):
    """The terminal service is configured but not answering."""

    code = "link_unavailable"
    status = 503


def error_from_payload(payload: Any, status: int) -> TerminalError:
    """Rebuild the link error a remote service reported."""
    code = payload.get("code") if isinstance(payload, dict) else None
    message = (payload.get("error") if isinstance(payload, dict) else None) or ""
    for kind in (SessionGone, PTYUnsupported, SessionStartFailed, SessionWriteFailed,
                 LinkUnavailable):
        if kind.code == code:
            return kind(message or kind.code)
    if code == SessionLimitReached.code:
        return SessionLimitReached(message or code)
    return TerminalError(message or f"terminal service returned HTTP {status}")


# --------------------------------------------------------------------------- #
# Local link — the terminal manager running inside this process
# --------------------------------------------------------------------------- #


class LocalLink:
    """`terminal.pty.manager` behind the async link surface.

    Every call that touches a PTY is pushed onto a worker thread: `create()`
    forks, `write()` blocks on the tty, and the stream loop would otherwise
    stall the event loop the dashboard is served from.
    """

    remote = False

    # ------------------------------ state ------------------------------ #

    async def snapshot(self) -> dict:
        # No shells can exist without a PTY, so the empty state stays a
        # normal listing: the tab bar renders and the create call reports why.
        return manager.snapshot()

    async def get(self, session: str | None) -> dict | None:
        if not session:
            return None
        found = manager.get(session)
        return None if found is None else found.info(active=found.id == manager.active_id)

    async def create(self, *, cwd: str = "", cols: int = 120, rows: int = 32,
                     label: str = "") -> dict:
        def _create():
            try:
                return manager.create(cwd=cwd, cols=cols, rows=rows, label=label)
            except SessionLimitReached:
                raise
            except PTYUnavailable as err:
                raise _as_link_error(err) from err
            except OSError as err:
                raise SessionStartFailed(f"Could not start a shell: {err}") from err

        return (await asyncio.to_thread(_create)).info(active=True)

    async def activate(self, session: str) -> dict | None:
        found = await asyncio.to_thread(manager.activate, session)
        return None if found is None else found.info(active=True)

    async def rename(self, session: str, label: str) -> dict | None:
        found = await asyncio.to_thread(manager.rename, session, label)
        if found is None:
            return None
        return found.info(active=found.id == manager.active_id)

    async def write(self, session: str, data: str) -> None:
        found = manager.get(session)
        if found is None or found.closed:
            raise SessionGone("That session is no longer running")
        try:
            await asyncio.to_thread(found.write, data)
        except RuntimeError as err:
            raise SessionWriteFailed(str(err)) from err

    async def resize(self, session: str, cols: int, rows: int) -> tuple[int, int]:
        found = manager.get(session)
        if found is None or found.closed:
            raise SessionGone("That session is no longer running")
        await asyncio.to_thread(found.resize, cols, rows)
        return found.cols, found.rows

    async def close(self, session: str) -> bool:
        return await asyncio.to_thread(manager.close, session)

    # ------------------------------ stream ----------------------------- #

    async def stream(self, session: str, offset: int,
                     disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
        """Portable SSE: the reader thread fills the scrollback, this loop
        hands out everything after the client cursor and announces state
        changes (cd, exit) as discrete events."""
        sess = manager.get(session)
        if sess is None:
            raise SessionGone("That session is no longer running")

        cursor = max(0, int(offset))
        last_cwd = sess.cwd
        backlog, cursor = sess.snapshot(cursor)
        yield "data: " + json.dumps({
            "session": sess.id,
            "label": sess.label,
            "cwd": last_cwd,
            "cols": sess.cols,
            "rows": sess.rows,
            "o": backlog.decode("utf-8", "replace"),
        }) + "\n\n"

        while not sess.closed:
            if await disconnected():
                break
            event: dict = {}
            data, size = sess.snapshot(cursor)
            if data:
                cursor = size
                event["o"] = data.decode("utf-8", "replace")
            if sess.cwd != last_cwd:
                last_cwd = sess.cwd
                event["cwd"] = last_cwd
                event["label"] = sess.label
            if event:
                yield "data: " + json.dumps(event) + "\n\n"
            else:
                await asyncio.sleep(0.08)
                yield ": keep-alive\n\n"
        yield "data: " + json.dumps({
            "exit": sess.exit_code if sess.exit_code is not None else 0,
            "done": True,
        }) + "\n\n"

    # ------------------------------ agent ------------------------------ #

    async def run_command(self, command: str, label: str | None = None,
                          timeout: int | None = None) -> tuple[str, int] | None:
        """Run one command in the agent's own labelled tab."""
        def _run():
            try:
                sess = agent_session(label)
            except PTYUnavailable:
                return None          # no shells on this host; the agent falls back
            if sess is None or sess.closed:
                return None
            return run_in_session(sess, command, timeout) if timeout \
                else run_in_session(sess, command)

        return await asyncio.to_thread(_run)

    # ---------------------------- lifecycle ---------------------------- #

    async def ensure_default(self) -> None:
        try:
            await asyncio.to_thread(ensure_default)
        except PTYUnavailable:
            return               # a host without PTY simply has no default shell

    async def close_all(self) -> None:
        await asyncio.to_thread(manager.close_all)

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Remote link — the terminal hosted as its own service
# --------------------------------------------------------------------------- #


class RemoteLink:
    """Talks to a separately hosted terminal over HTTP + SSE.

    The service runs the very same routes (`terminal/api.py` over a
    LocalLink), so every response here has the shape the dashboard already
    knows. `transport` exists so the offline test suite can drive the whole
    client through an `httpx.MockTransport`.
    """

    remote = True

    def __init__(self, base_url: str, token: str = "", transport: Any = None):
        self._base = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if token:
            # The service hands out root shells — it must never be open.
            headers["X-Nova-Terminal-Token"] = token
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=CALL_TIMEOUT,
            transport=transport,
            trust_env=False,
        )

    # ----------------------------- plumbing ---------------------------- #

    async def _call(self, method: str, path: str, *, json_body: Any = None,
                    params: dict | None = None, timeout: Any = None) -> Any:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if params:
            kwargs["params"] = params
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            res = await self._client.request(method, f"{REMOTE_PREFIX}{path}", **kwargs)
        except httpx.HTTPError as err:
            raise LinkUnavailable(
                f"The terminal service at {self._base} is not answering "
                f"({err.__class__.__name__})."
            ) from err
        if res.status_code >= 400:
            try:
                payload = res.json()
            except ValueError:
                payload = {}
            raise error_from_payload(payload, res.status_code)
        return res.json() if res.content else None

    @staticmethod
    def _session_of(snapshot: Any, session: str) -> dict | None:
        if not isinstance(snapshot, dict):
            return None
        for found in snapshot.get("sessions") or []:
            if isinstance(found, dict) and found.get("id") == session:
                return found
        return None

    # ------------------------------ state ------------------------------ #

    async def snapshot(self) -> dict:
        return await self._call("GET", "/sessions")

    async def get(self, session: str | None) -> dict | None:
        if not session:
            return None
        return self._session_of(await self.snapshot(), session)

    async def create(self, *, cwd: str = "", cols: int = 120, rows: int = 32,
                     label: str = "") -> dict:
        return (await self._call("POST", "/sessions",
                                 json_body={"cwd": cwd, "cols": cols,
                                            "rows": rows, "label": label}))["info"]

    async def activate(self, session: str) -> dict | None:
        payload = await self._call("POST", "/activate", json_body={"session": session})
        return payload.get("info") if payload else None

    async def rename(self, session: str, label: str) -> dict | None:
        payload = await self._call("POST", "/rename",
                                   json_body={"session": session, "label": label})
        return payload.get("info") if payload else None

    async def write(self, session: str, data: str) -> None:
        await self._call("POST", "/input", json_body={"session": session, "data": data})

    async def resize(self, session: str, cols: int, rows: int) -> tuple[int, int]:
        payload = await self._call("POST", "/resize",
                                   json_body={"session": session, "cols": cols, "rows": rows})
        return int(payload.get("cols", cols)), int(payload.get("rows", rows))

    async def close(self, session: str) -> bool:
        await self._call("POST", "/stop", json_body={"session": session})
        return True

    # ------------------------------ stream ----------------------------- #

    async def stream(self, session: str, offset: int,
                     disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
        """Forward the service's SSE frames verbatim — they are already the
        frames the dashboard expects, so re-encoding would only add drift."""
        params = {"session": session, "offset": max(0, int(offset))}
        try:
            async with self._client.stream(
                "GET", f"{REMOTE_PREFIX}/stream", params=params, timeout=STREAM_TIMEOUT
            ) as res:
                if res.status_code >= 400:
                    await res.aread()
                    try:
                        payload = res.json()
                    except ValueError:
                        payload = {}
                    raise error_from_payload(payload, res.status_code)
                async for chunk in res.aiter_text():
                    if await disconnected():
                        break
                    if chunk:
                        yield chunk
        except httpx.HTTPError as err:
            raise LinkUnavailable(
                f"The terminal service at {self._base} dropped the stream "
                f"({err.__class__.__name__})."
            ) from err

    # ------------------------------ agent ------------------------------ #

    async def run_command(self, command: str, label: str | None = None,
                          timeout: int | None = None) -> tuple[str, int] | None:
        payload = await self._call(
            "POST", "/run",
            json_body={"command": command, "label": label, "timeout": timeout},
            timeout=(float(timeout) + 15.0) if timeout else None,
        )
        if not payload or not payload.get("ok"):
            return None
        return str(payload.get("output") or ""), int(payload.get("code", -1))

    # ---------------------------- lifecycle ---------------------------- #

    async def ensure_default(self) -> None:
        # The service keeps its own permanent Root@Build shell alive; there is
        # nothing for the app to warm up on this side.
        return None

    async def close_all(self) -> None:
        # Sessions outlive the app on purpose — a redeploy of the gateway must
        # not kill the shells the user is typing into.
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# Resolver — which link this process uses
# --------------------------------------------------------------------------- #

_local = LocalLink()
_remote: RemoteLink | None = None
_pinned: TerminalLink | None = None


def _remote_link() -> RemoteLink:
    global _remote
    if _remote is None:
        _remote = RemoteLink(config.TERMINAL_SERVICE_URL,
                             token=config.TERMINAL_SERVICE_TOKEN)
        log.info("terminal link: remote service at %s", config.TERMINAL_SERVICE_URL)
    return _remote


def current() -> TerminalLink:
    """The link every caller goes through. Resolved per call so config and
    tests both apply without a reload."""
    if _pinned is not None:
        return _pinned
    if config.TERMINAL_SERVICE_URL:
        return _remote_link()
    return _local


def pin(link: TerminalLink | None) -> None:
    """Force a specific link — the standalone service always pins LocalLink
    (it owns its shells no matter what the environment says), and the test
    suite pins a MockTransport-backed client."""
    global _pinned
    _pinned = link


def is_remote() -> bool:
    return current().remote


def shell_hint_for(command: str) -> str | None:
    """One-shot exec hint: TTY-requiring flows need the live terminal.

    The hint is a property of the command, not of where the terminal runs, so
    it is answered locally in both modes.
    """
    return shell_hint(command)
