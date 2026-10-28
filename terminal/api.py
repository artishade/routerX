"""The terminal's HTTP contract — mounted by BOTH hosts.

  main.py              → /api/admin/terminal/pty/*   (the dashboard's API)
  terminal/service.py  → /terminal/pty/*             (standalone service)

Both mount *this* router over the same `terminal.link` surface, so a
terminal hosted separately is not a reimplementation of these routes — it is
the same routes, with the local link underneath. That is what keeps the two
hosts from drifting: every response shape, status code and SSE frame is defined
exactly once, here.

Every handler resolves the link per request (`terminal.link.current()`), so
the standalone service can pin itself to the local link and the app can switch
between in-process and remote purely from configuration.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from terminal import link
from terminal.link import PTYUnsupported, SessionGone, SessionLimitReached, TerminalError
from terminal.pty import PTY_SUPPORTED

router = APIRouter()


def _fail(err: TerminalError) -> JSONResponse:
    """One error shape for both hosts: `{"error": …, "code": …}`.

    `code` is how a remote link rebuilds the exact exception, so a failure
    raised on the other side of the link reads the same to the dashboard.
    """
    return JSONResponse({"error": str(err), "code": err.code}, status_code=err.status)


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- #
# GET  /sessions → one snapshot of every session (the client renders tabs)
# --------------------------------------------------------------------------- #


@router.get("/sessions")
async def sessions():
    terminal = link.current()
    try:
        return await terminal.snapshot()
    except TerminalError as err:
        return _fail(err)


# --------------------------------------------------------------------------- #
# POST /sessions → open a session and focus it
# --------------------------------------------------------------------------- #


@router.post("/sessions")
async def create(request: Request):
    body = await _body(request)
    terminal = link.current()
    try:
        info = await terminal.create(
            cwd=body.get("cwd") if isinstance(body.get("cwd"), str) else "",
            cols=body.get("cols") if isinstance(body.get("cols"), int) else 120,
            rows=body.get("rows") if isinstance(body.get("rows"), int) else 32,
            label=body.get("label") if isinstance(body.get("label"), str) else "",
        )
    except SessionLimitReached as err:
        return _fail(err)
    except TerminalError as err:
        return _fail(err)
    return {"ok": True, "session": info.get("id"), "info": info}


# --------------------------------------------------------------------------- #
# POST /activate → switch the focused session
# --------------------------------------------------------------------------- #


@router.post("/activate")
async def activate(request: Request):
    body = await _body(request)
    session = body.get("session")
    if not isinstance(session, str):
        return JSONResponse({"error": "session is required", "code": "bad_request"},
                            status_code=400)
    try:
        info = await link.current().activate(session)
    except TerminalError as err:
        return _fail(err)
    if info is None:
        return _fail(SessionGone("That session is no longer running"))
    return {"ok": True, "info": info}


# --------------------------------------------------------------------------- #
# POST /rename → name a session tab
# --------------------------------------------------------------------------- #


@router.post("/rename")
async def rename(request: Request):
    body = await _body(request)
    session, label = body.get("session"), body.get("label")
    if not isinstance(session, str) or not isinstance(label, str):
        return JSONResponse({"error": "session and label are required", "code": "bad_request"},
                            status_code=400)
    try:
        info = await link.current().rename(session, label)
    except TerminalError as err:
        return _fail(err)
    if info is None:
        return _fail(SessionGone("That session is no longer running"))
    return {"ok": True, "info": info}


# --------------------------------------------------------------------------- #
# GET  /stream → SSE: output, working-directory and exit events
# --------------------------------------------------------------------------- #


@router.get("/stream")
async def stream(request: Request, session: str, offset: int = 0):
    terminal = link.current()
    try:
        if await terminal.get(session) is None:
            return _fail(SessionGone("That session is no longer running"))
    except TerminalError as err:
        return _fail(err)

    async def gen():
        try:
            async for frame in terminal.stream(session, offset, request.is_disconnected):
                yield frame
        except TerminalError as err:
            # Headers are already sent by then, so the failure rides the stream
            # as one last event rather than a status code.
            yield "data: " + json.dumps(
                {"error": str(err), "code": err.code, "done": True}
            ) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# POST /input → keystrokes (\r, \u0003, arrows …)
# --------------------------------------------------------------------------- #


@router.post("/input")
async def write(request: Request):
    body = await _body(request)
    session, data = body.get("session"), body.get("data")
    if not isinstance(session, str) or not isinstance(data, str):
        return JSONResponse({"error": "session and data are required", "code": "bad_request"},
                            status_code=400)
    try:
        await link.current().write(session, data)
    except TerminalError as err:
        return _fail(err)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# POST /resize → cols/rows
# --------------------------------------------------------------------------- #


@router.post("/resize")
async def resize(request: Request):
    body = await _body(request)
    session = body.get("session")
    if not isinstance(session, str):
        return JSONResponse({"error": "session is required", "code": "bad_request"},
                            status_code=400)
    terminal = link.current()
    info = await terminal.get(session)
    if info is None:
        return _fail(SessionGone("That session is no longer running"))
    try:
        cols, rows = await terminal.resize(
            session,
            body.get("cols") if isinstance(body.get("cols"), int) else info.get("cols", 120),
            body.get("rows") if isinstance(body.get("rows"), int) else info.get("rows", 32),
        )
    except TerminalError as err:
        return _fail(err)
    return {"ok": True, "cols": cols, "rows": rows}


# --------------------------------------------------------------------------- #
# POST /stop → close a session
# --------------------------------------------------------------------------- #


@router.post("/stop")
async def stop(request: Request):
    body = await _body(request)
    session = body.get("session")
    if not isinstance(session, str):
        return JSONResponse({"error": "session is required", "code": "bad_request"},
                            status_code=400)
    terminal = link.current()
    try:
        closed = await terminal.close(session)
    except TerminalError as err:
        return _fail(err)
    if not closed:
        return _fail(SessionGone("That session is no longer running"))
    return {"ok": True, "state": await terminal.snapshot()}


# --------------------------------------------------------------------------- #
# POST /run → one command in the agent's own tab (the autonomous agent's
#              shell step, so a task still runs where the user can watch it)
# --------------------------------------------------------------------------- #


@router.post("/run")
async def run(request: Request):
    body = await _body(request)
    command = body.get("command")
    if not isinstance(command, str) or not command.strip():
        return JSONResponse({"error": "command is required", "code": "bad_request"},
                            status_code=400)
    label = body.get("label") if isinstance(body.get("label"), str) else None
    timeout = body.get("timeout") if isinstance(body.get("timeout"), int) else None
    try:
        result = await link.current().run_command(command, label, timeout)
    except TerminalError as err:
        return _fail(err)
    if result is None:
        if not PTY_SUPPORTED:
            # Honest cause: there is no shell to run in, and none can start.
            return _fail(PTYUnsupported(
                "interactive PTY sessions need a POSIX host (Linux/macOS); "
                "the sandbox command path still works."
            ))
        return _fail(SessionGone("no terminal session is available"))
    output, code = result
    return {"ok": True, "output": output, "code": code}
