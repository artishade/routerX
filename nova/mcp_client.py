"""Custom MCP servers — the console's plugin system.

A user registers a Model Context Protocol server (a "plugin") and its tools
become callable tools in the RouterX Console and in the autonomous agent, exactly
like the permanent builtin toolbox. Two transports are supported:

  http    Streamable HTTP JSON-RPC (the 2025-06-18 spec every current server
          speaks): one POST per message, reply either as JSON or as an
          SSE-framed `data:` payload. A returned `Mcp-Session-Id` is echoed on
          the following calls, as the spec requires.
  stdio   A local process speaking newline-delimited JSON-RPC on stdin/stdout
          (`npx -y @scope/server`, `uvx …`, `python3 -m …`). Processes are
          cached per server so several agent steps reuse one boot, restarted
          automatically when the child dies, and capped at MAX_STDIO_SESSIONS.

Everything here is fail-soft: a broken plugin reports an honest error string
and never takes the gateway down. Nothing is executed through a shell — stdio
commands are argv-split and run with exec, no `shell=True`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from typing import Any

import httpx

log = logging.getLogger("nova.mcp")

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "routerx-console", "version": "1.0"}
TRANSPORTS = ("http", "stdio")
DEFAULT_TIMEOUT = 25.0
CALL_TIMEOUT = 60.0
MAX_STDIO_SESSIONS = 8
STDIO_BOOT_GRACE = 0.35   # how long a fresh plugin process gets to die on us
MAX_TOOLS = 200

# Header names whose value is a credential and must never be echoed back.
_SECRET_HEADER_HINTS = ("auth", "token", "key", "secret", "cookie", "session")


class McpError(RuntimeError):
    """Any MCP failure — surfaced to the UI/agent as a readable message."""


# --------------------------------------------------------------------------- #
# Server spec helpers
# --------------------------------------------------------------------------- #

def _loads(raw: Any, fallback) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        value = json.loads(raw or "")
    except Exception:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def spec_from_row(row) -> dict:
    """ORM row or plain dict → the dict the client works with."""
    get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
    return {
        "id": str(get("id") or ""),
        "name": str(get("name") or get("id") or "mcp"),
        "transport": (str(get("transport") or "http")).lower(),
        "url": str(get("url") or ""),
        "command": str(get("command") or ""),
        "headers": _loads(get("headers"), {}),
        "allow": [str(a) for a in _loads(get("allow"), [])],
        "enabled": bool(get("enabled", True)),
    }


def mask_headers(headers: dict) -> dict:
    """`{"Authorization": "Bearer abc…"}` → `{"Authorization": "Bearer ••••"}`."""
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        text = "" if value is None else str(value)
        if any(hint in str(key).lower() for hint in _SECRET_HEADER_HINTS):
            keep = text.split(" ")[0] if text.lower().startswith("bearer ") else ""
            out[str(key)] = (keep + " ••••" if keep else "••••") or "••••"
        else:
            out[str(key)] = text
    return out


def tool_ref(server: dict, tool: dict) -> dict:
    """One discovered tool, flattened for the agent/UI catalogue."""
    name = str(tool.get("name") or "").strip()
    return {
        "server": server["id"],
        "server_name": server["name"],
        "name": name,
        "qualified": f"{server['id']}::{name}" if name else server["id"],
        "description": str(tool.get("description") or "")[:400],
        "schema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
    }


def _allowed(server: dict, tools: list[dict]) -> list[dict]:
    allow = set(server.get("allow") or [])
    picked = [t for t in tools if not allow or t.get("name") in allow]
    return picked[:MAX_TOOLS]


# --------------------------------------------------------------------------- #
# JSON-RPC envelope
# --------------------------------------------------------------------------- #

_id_counter = 0


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter


def _envelope(method: str, params: dict | None = None, rpc_id: int | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        message["id"] = rpc_id
    if params is not None:
        message["params"] = params
    return message


def _unwrap(message: Any) -> dict:
    """Pull the `result` out of a JSON-RPC response, raising on protocol errors."""
    if not isinstance(message, dict):
        raise McpError(f"unexpected reply: {str(message)[:160]}")
    if "error" in message and message["error"]:
        err = message["error"]
        detail = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        raise McpError(f"MCP error{f' {code}' if code else ''}: {str(detail)[:200]}")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


def _pick_reply(frames: list[Any], rpc_id: int) -> dict | None:
    """The response for `rpc_id`, ignoring requests/notifications around it."""
    for frame in frames:
        if isinstance(frame, dict) and frame.get("id") == rpc_id:
            return frame
    return None


def _sse_frames(body: str) -> list[Any]:
    """Pull the JSON-RPC payloads out of an SSE-framed response body."""
    frames: list[Any] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[5:].lstrip() for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            frames.append(json.loads("\n".join(data_lines)))
        except Exception:
            continue
    return frames


# --------------------------------------------------------------------------- #
# Streamable HTTP transport
# --------------------------------------------------------------------------- #

_http_sessions: dict[str, str] = {}   # server id -> Mcp-Session-Id


def _http_headers(server: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update({str(k): str(v) for k, v in (server.get("headers") or {}).items()})
    session_id = _http_sessions.get(server["id"])
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


async def _rpc_http(server: dict, message: dict, timeout: float) -> dict | None:
    url = server.get("url") or ""
    if not url.lower().startswith(("http://", "https://")):
        raise McpError(f"{server['id']}: URL must start with http:// or https://")
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
        res = await client.post(url, json=message, headers=_http_headers(server))
    session_id = res.headers.get("mcp-session-id")
    if session_id:
        _http_sessions[server["id"]] = session_id
    if res.status_code >= 400:
        detail = res.text[:200].strip()
        raise McpError(f"{server['id']}: HTTP {res.status_code} {detail}")
    if not res.content or res.status_code == 202:
        return None                      # a notification was accepted
    content_type = res.headers.get("content-type", "")
    if "text/event-stream" in content_type or res.text.lstrip().startswith(("event:", "data:")):
        frames = _sse_frames(res.text)
        return _pick_reply(frames, message.get("id", -1))
    try:
        payload = res.json()
    except Exception as exc:
        raise McpError(f"{server['id']}: reply was not JSON ({exc.__class__.__name__})") from exc
    if isinstance(payload, list):
        return _pick_reply(payload, message.get("id", -1))
    if isinstance(payload, str) and ("data:" in payload or payload.lstrip().startswith("event:")):
        # Some servers (and proxies in front of them) frame SSE but label it
        # application/json — the payload then arrives as a JSON string.
        return _pick_reply(_sse_frames(payload), message.get("id", -1))
    return payload


# --------------------------------------------------------------------------- #
# stdio transport — one cached child process per server
# --------------------------------------------------------------------------- #

_DRIVE_PATH_RE = re.compile(r"^(?P<drive>[A-Za-z]):(?P<rest>[^/\\].*)$")
_SURROUNDING_QUOTES = ("\"", "'")


def _split_command(raw: str) -> list[str]:
    """Split an admin-supplied stdio command into argv, shell-free.

    `shlex` must keep backslashes on Windows, or a POSIX split eats the
    separators in `C:\\Users\\...\\python.exe` and the spawn fails with
    `[WinError 2]`. A mangled drive path is also repaired when — and only
    when — the repaired path exists on disk, so a genuine `C:name`
    argument is never guessed at.
    """
    posix = os.name != "nt"
    argv = [
        _repair_drive_path(_unquote(part, posix))
        for part in shlex.split(raw, posix=posix)
    ]
    return argv


def _unquote(arg: str, posix: bool) -> str:
    """`shlex` keeps a leading quote in non-POSIX mode; a lone quoted path
    (the common Windows case) would otherwise reach exec with its quotes."""
    if posix or len(arg) < 2:
        return arg
    if arg[0] in _SURROUNDING_QUOTES and arg[-1] == arg[0]:
        return arg[1:-1]
    return arg


def _repair_drive_path(arg: str) -> str:
    match = _DRIVE_PATH_RE.match(arg)
    if not match:
        return arg
    # `C:name` is a drive-relative path, so a bare existence check is not
    # enough: the repaired form must be an absolute file that exists.
    repaired = match.group("drive") + ":\\" + match.group("rest")
    return repaired if os.path.isfile(repaired) else arg


class _StdioSession:
    def __init__(self, server: dict):
        self.server = server
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.touched = time.time()

    def _argv(self) -> list[str]:
        raw = (self.server.get("command") or "").strip()
        if not raw:
            raise McpError(f"{self.server['id']}: no command configured")
        try:
            argv = _split_command(raw)
        except ValueError as exc:
            raise McpError(f"{self.server['id']}: bad command ({exc})") from exc
        if not argv:
            raise McpError(f"{self.server['id']}: no command configured")
        return argv

    async def _start(self) -> asyncio.subprocess.Process:
        argv = self._argv()
        env = {
            "PATH": __import__("os").environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": __import__("os").environ.get("HOME", "/root"),
            # Plugins are part of the agent toolbox, so they get the same
            # identity the workspace shell does.
            "NOVA_GATEWAY": "1",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"{self.server['id']}: cannot start `{argv[0]}` ({exc})") from exc
        deadline = time.time() + STDIO_BOOT_GRACE
        while time.time() < deadline:
            if process.returncode is not None:
                stderr = b""
                if process.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(process.stderr.read(512), timeout=2)
                    except Exception:
                        stderr = b""
                detail = stderr.decode("utf-8", "replace").strip()[:200]
                raise McpError(
                    f"{self.server['id']}: `{argv[0]}` exited with code "
                    f"{process.returncode}{' — ' + detail if detail else ''}"
                )
            await asyncio.sleep(0.02)
        self.process = process
        return process

    async def _read_reply(self, process: asyncio.subprocess.Process, rpc_id: int) -> dict | None:
        assert process.stdout is not None
        deadline = time.time() + self._read_timeout
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            frames = _sse_frames(text) if text.startswith(("event:", "data:")) else []
            candidates = frames or [text]
            for candidate in candidates:
                try:
                    payload = json.loads(candidate)
                except Exception:
                    continue
                if isinstance(payload, dict) and payload.get("id") == rpc_id:
                    return payload
        return None

    _read_timeout = DEFAULT_TIMEOUT

    async def send(self, message: dict, timeout: float) -> dict | None:
        async with self.lock:
            self.touched = time.time()
            self._read_timeout = timeout
            process = self.process
            if process is None or process.returncode is not None:
                process = await self._start()
            assert process.stdin is not None
            line = json.dumps(message, separators=(",", ":")) + "\n"
            try:
                process.stdin.write(line.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                self.process = None
                raise McpError(f"{self.server['id']}: the plugin process went away ({exc})") from exc
            if message.get("id") is None:
                return None                # notification: nothing to wait for
            reply = await self._read_reply(process, message["id"])
            if reply is None:
                self.process = None
                raise McpError(f"{self.server['id']}: no answer within {int(timeout)}s")
            return reply

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass


_sessions: dict[str, _StdioSession] = {}


async def _rpc_stdio(server: dict, message: dict, timeout: float) -> dict | None:
    session = _sessions.get(server["id"])
    if session is None:
        session = _sessions[server["id"]] = _StdioSession(server)
        await _evict_stdio()
    return await session.send(message, timeout)


async def _evict_stdio() -> None:
    while len(_sessions) > MAX_STDIO_SESSIONS:
        oldest = min(_sessions.values(), key=lambda s: s.touched)
        _sessions.pop(oldest.server["id"], None)
        await oldest.close()


async def close_all() -> None:
    """Shutdown hook — every plugin process is torn down with the gateway."""
    for session in list(_sessions.values()):
        _sessions.pop(session.server["id"], None)
        await session.close()
    _http_sessions.clear()


async def rpc(server: dict, method: str, params: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One JSON-RPC call, with the MCP handshake when the server needs it."""
    transport = server.get("transport") or "http"
    send = _rpc_stdio if transport == "stdio" else _rpc_http
    if not server.get("_ready"):
        init_id = _next_id()
        _unwrap(await send(server, _envelope(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            init_id,
        ), timeout) or {})
        await send(server, _envelope("notifications/initialized"), timeout)
        server["_ready"] = True
    rpc_id = _next_id()
    reply = await send(server, _envelope(method, params or {}, rpc_id), timeout)
    if reply is None:
        raise McpError(f"{server['id']}: {method} returned no result")
    return _unwrap(reply)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

async def list_tools(server: dict, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Every tool the plugin exposes, after the user's allow-list filter."""
    try:
        result = await rpc(server, "tools/list", {}, timeout)
    except McpError:
        server.pop("_ready", None)     # a stale handshake must not stick
        result = await rpc(server, "tools/list", {}, timeout)
    tools = result.get("tools")
    return _allowed(server, [t for t in tools if isinstance(t, dict)]) if isinstance(tools, list) else []


async def call_tool(server: dict, name: str, arguments: dict | None = None,
                    timeout: float = CALL_TIMEOUT) -> str:
    """Call one tool and return its content as plain text for the agent log."""
    allowed = server.get("allow") or []
    if allowed and name not in allowed:
        listed = ", ".join(sorted(allowed))
        raise McpError(f"{server['id']}: `{name}` is not enabled for this server (allowed: {listed})")
    result = await rpc(server, "tools/call", {"name": name, "arguments": arguments or {}}, timeout)
    if result.get("isError"):
        raise McpError(_content_text(result) or f"{server['id']}: {name} reported an error")
    text = _content_text(result)
    return text or "(no output)"


def _content_text(result: dict) -> str:
    """Flatten an MCP tool result: text blocks, then images/resources as JSON."""
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {len(str(block.get('data') or ''))} bytes]")
        else:
            parts.append(json.dumps(block, separators=(",", ":"))[:400])
    return "\n".join(p for p in parts if p).strip()


async def probe(server: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Connect and report what the plugin offers — used by the UI's Test button."""
    started = time.time()
    try:
        tools = await list_tools(server, timeout)
        return {
            "ok": True,
            "tools": tools,
            "count": len(tools),
            "latency_ms": int((time.time() - started) * 1000),
            "error": None,
        }
    except McpError as exc:
        server.pop("_ready", None)
        return {
            "ok": False,
            "tools": [],
            "count": 0,
            "latency_ms": int((time.time() - started) * 1000),
            "error": str(exc),
        }
    except Exception as exc:  # pragma: no cover — defensive
        server.pop("_ready", None)
        return {
            "ok": False,
            "tools": [],
            "count": 0,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{exc.__class__.__name__}: {exc}"[:200],
        }


def parse_call(argument: str) -> tuple[str, str, dict]:
    """Accept the three shapes a planner may produce for a plugin call.

    1. `server::tool {"json": "args"}`     (canonical)
    2. `server::tool`                     (no arguments)
    3. `{"server": …, "tool": …, "arguments": {…}}`   (a bare JSON object)
    """
    raw = (argument or "").strip()
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            server = str(payload.get("server") or "").strip()
            tool = str(payload.get("tool") or payload.get("name") or "").strip()
            args = payload.get("arguments") or payload.get("args") or {}
            if server and tool:
                return server, tool, args if isinstance(args, dict) else {}
        except Exception:
            pass
    if "::" not in raw:
        raise McpError("use `server::tool` or {\"server\":…,\"tool\":…}")
    server_id, _, rest = raw.partition("::")
    tool, _, args_raw = rest.strip().partition(" ")
    if not server_id or not tool:
        raise McpError("use `server::tool` or {\"server\":…,\"tool\":…}")
    args: dict = {}
    args_raw = args_raw.strip()
    if args_raw:
        try:
            parsed = json.loads(args_raw)
            args = parsed if isinstance(parsed, dict) else {"input": parsed}
        except Exception:
            args = {"input": args_raw}
    return server_id.strip(), tool.strip(), args