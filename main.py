"""RouterX — Python server entrypoint (FastAPI + uvicorn).

Binds to 0.0.0.0:$PORT (Render assigns PORT dynamically; local default 3000).
Owns:
  • /health, /api/health          — health checks for uptime pings
  • /v1/* + /api/v1/*             — OpenAI-compatible gateway (with CORS)
  • /api/admin/* , /api/agent/*   — JSON API (source of truth for the UI)
  • / and /partials/tab/*         — the Python dashboard UI (Jinja2 + HTMX BFF)
                                    rendered by the `ui` package from Python —
                                    no Node/React frontend anymore.

The frontend module is Python: `ui/` renders templates server-side and calls
this same JSON API (self-BFF), so UI behaviour always matches the gateway.

Terminal: nothing terminal lives in this file. Everything terminal-related is
under `terminal/`, imported here as `terminal.link` — so the interactive
terminal can be deployed separately (see `terminal/README.md`).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRouter
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from nova import auth as nova_auth
from nova import engine as nova_engine
from maintenance import periodic_maintenance
from nova.bootstrap import run_bootstrap
from nova.config import CORS_ALLOW_ORIGINS, IS_POSTGRES, PORT, PROJECT_ROOT
from nova.database import ensure_sqlite_dir, ping
# The terminal is a separate feature under `terminal/` — in-process by default,
# or a service of its own when NOVA_TERMINAL_URL points at one.
from terminal import link as terminal_link

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nova.main")

STARTED_AT = time.time()


EPHEMERAL_DB_WARNING = (
    "SQLite lives inside the ephemeral container — every deploy (git push) or "
    "restart wipes providers, models, keys, routes and configs. Set DATABASE_URL "
    "to a managed Postgres URL (free at neon.tech) in the Render dashboard to "
    "keep your data forever."
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_sqlite_dir()
    run_bootstrap()
    if not IS_POSTGRES:
        log.warning("=" * 74)
        log.warning("EPHEMERAL DATABASE — no Postgres DATABASE_URL configured.")
        log.warning("%s", EPHEMERAL_DB_WARNING)
        log.warning("=" * 74)
    # Permanent cloud terminal: the Root@Build shell always exists and the
    # agent toolbox + cloud build targets are always registered — background
    # agent tasks can run at any time without any UI action.
    try:
        from nova.build_registry import ensure_build_providers, register_builtin_tools
        from nova.database import SessionLocal

        with SessionLocal() as _db:
            ensure_build_providers(_db)
            register_builtin_tools(_db)
    except Exception as err:  # never block boot on registry bookkeeping
        log.warning("build registry warmup skipped: %s", err)
    try:
        from terminal import link as terminal_link
        await terminal_link.current().ensure_default()
    except Exception as err:
        log.warning("default cloud shell warmup skipped: %s", err)
    await nova_engine.start_sidecar()
    # Start periodic maintenance task (sync + disable dead) every 12h
    asyncio.create_task(periodic_maintenance())
    # Usage windows (nova.ratelimit) keep 2 days; prune hourly. The gateway also
    # prunes lazily (at most once an hour) when no loop has run yet.
    asyncio.create_task(usage_maintenance())
    log.info("RouterX up on 0.0.0.0:%s (Python UI + API)", PORT)
    yield
    try:
        from ui.api_client import api as _ui_api
        await _ui_api.close()
    except Exception:
        pass
    try:
        from terminal import link as terminal_link
        await terminal_link.current().close_all()
        await terminal_link.current().aclose()
    except Exception:
        pass
    try:
        from nova import mcp_client
        await mcp_client.close_all()
    except Exception:
        pass
    await nova_engine.stop_sidecar()


app = FastAPI(
    title="RouterX Gateway",
    version="2.0.0-python",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redirect_slashes=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,       # requirement #4 — browser clients allowed
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Gateway request logging — every /v1/* and /api/v1/* call gets a line with
# method, path, client, status, duration and (for /v1/messages) a compact
# summary of what Claude Code actually sent (tools? tool_result blocks?).
# Errors and 4xx/5xx are logged at WARNING with the response body snippet so
# agent-session failures can be diagnosed without attaching a debugger.
# --------------------------------------------------------------------------- #

GATEWAY_PREFIXES = ("/v1", "/api/v1")

def _summarize_anthropic_payload(payload: dict) -> str:
    msgs = payload.get("messages") or []
    tools = payload.get("tools") or []
    n_tool_use = n_tool_result = n_thinking = 0
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    bt = b.get("type")
                    if bt == "tool_use": n_tool_use += 1
                    elif bt == "tool_result": n_tool_result += 1
                    elif bt in ("thinking", "redacted_thinking"): n_thinking += 1
    tool_names = ",".join(str(t.get("name", "")) for t in tools if isinstance(t, dict))[:120]
    return (
        f"model={payload.get('model')} stream={payload.get('stream')} "
        f"msgs={len(msgs)} tool_defs={len(tools)}[{tool_names}] "
        f"tool_use={n_tool_use} tool_result={n_tool_result} thinking={n_thinking} "
        f"max_tokens={payload.get('max_tokens')}"
    )

@app.middleware("http")
async def gateway_request_log(request: Request, call_next):
    path = request.url.path
    if not path.startswith(GATEWAY_PREFIXES):
        return await call_next(request)
    # A stable id on every gateway response makes a hosted failure traceable
    # from the agent's error message to the server log without guessing.
    request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:32]
    request.state.request_id = request_id
    started = time.time()
    body_snip = ""
    if path.endswith("/messages") or path.endswith("/count_tokens"):
        try:
            raw = await request.body()
            if raw:
                import json as _json
                try:
                    body_snip = _summarize_anthropic_payload(_json.loads(raw))
                except Exception:
                    body_snip = f"<unparsed {len(raw)}B>"
                # FastAPI caches body for downstream handlers
                request._body = raw  # noqa: SLF001
        except Exception:
            body_snip = "<body read failed>"
    response = await call_next(request)
    dur_ms = int((time.time() - started) * 1000)
    response.headers["x-request-id"] = request_id
    principal = getattr(request.state, "client_principal", None)
    client_name = getattr(principal, "name", "") if principal is not None else ""
    line = (f"{request.client.host if request.client else '-'} "
            f"{request.method} {path} -> {response.status_code} {dur_ms}ms "
            f"request_id={request_id}")
    if client_name:
        line += f" client={client_name}"
    if body_snip:
        line += f" | {body_snip}"
    if response.status_code >= 400:
        log.warning("gateway %s", line)
    else:
        log.info("gateway %s", line)
    try:
        from nova import gwlog
        gwlog.record_request(
            request.client.host if request.client else None,
            request.method, path, response.status_code, dur_ms, body_snip,
        )
    except Exception:
        pass
    return response


async def usage_maintenance() -> None:
    """Prune old rate-limit windows hourly (keep 2 days). Never raises."""
    from nova import ratelimit as nova_ratelimit
    from nova.database import SessionLocal

    while True:
        await asyncio.sleep(3600)
        try:
            with SessionLocal() as db:
                removed = nova_ratelimit.prune_usage(db)
            if removed:
                log.info("usage maintenance: pruned %s old window(s)", removed)
        except Exception as err:  # noqa: BLE001 - maintenance must never kill the app
            log.warning("usage maintenance failed: %s", err)


@app.exception_handler(nova_auth.AuthError)
async def gateway_auth_error(request: Request, exc: nova_auth.AuthError):
    """401/403/429 from the client-key dependency, in the client's protocol shape.

    `nova.auth.gateway_auth` runs before the endpoint, so its refusals never
    reach the endpoint's own try/except. The RequestLog row is written here (the
    endpoint has not had a chance to), and `Retry-After` rides along for both
    401 and 429 so a well-behaved client backs off instead of hammering.
    """
    from nova.database import SessionLocal

    path = request.url.path
    request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex[:32]
    try:
        from nova.models import RequestLog

        with SessionLocal() as db:
            db.add(RequestLog(model="", providerName="", endpoint=path, status=exc.status,
                              latencyMs=0, via="auth", error=f"{exc.code}: {exc.message}"))
            db.commit()
    except Exception:
        pass
    if path.endswith("/messages"):
        body = {"type": "error", "error": {"type": "api_error", "message": exc.message, "code": exc.code}}
    else:
        body = exc.to_payload()
    headers = {**exc.headers(), "x-request-id": request_id}
    return JSONResponse(body, status_code=exc.status, headers=headers)


@app.exception_handler(Exception)
async def gateway_unhandled_exception(request: Request, exc: Exception):
    """Last-resort handler: gateway clients must never see a bare HTTP 500.

    Upstream/provider faults are handled inside the gateway; this catches
    anything that still escapes (schema drift, programming errors, DB outages)
    and answers in the client's own protocol shape with a request id.
    """
    path = request.url.path
    request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex[:32]
    if isinstance(exc, SQLAlchemyError):
        status, reason = 503, "Gateway database unavailable — retry shortly"
    elif isinstance(exc, asyncio.TimeoutError):
        status, reason = 504, "Gateway timed out waiting for a stage"
    else:
        status, reason = 502, f"Gateway internal error ({exc.__class__.__name__})"

    if path.startswith(GATEWAY_PREFIXES):
        log.exception("unhandled gateway error on %s %s (request_id=%s)", request.method, path, request_id)
        message = f"{reason}. request_id={request_id}"
        if path.endswith("/messages") or path.endswith("/count_tokens"):
            body = {"type": "error", "error": {"type": "api_error", "message": message}}
        else:
            body = {"error": {"message": message, "type": "gateway_error", "code": status}}
        return JSONResponse(body, status_code=status, headers={"x-request-id": request_id})

    log.exception("unhandled error on %s %s (request_id=%s)", request.method, path, request_id)
    return JSONResponse({"error": "Internal Server Error", "request_id": request_id}, status_code=500)


# --------------------------------------------------------------------------- #
# Health (requirement #3) — for Render uptime pings
# --------------------------------------------------------------------------- #

async def health_payload() -> dict:
    db_ok = await asyncio.to_thread(ping)
    engine_ok = await nova_engine.health_check(timeout=1.0)
    terminal_ok, terminal_host = await _terminal_health()
    return {
        "ok": db_ok and terminal_ok,
        "status": "healthy" if db_ok and terminal_ok else "degraded",
        "service": "routerx",
        "runtime": "python",
        "version": "2.0.0",
        "uptime_s": int(time.time() - STARTED_AT),
        "db": "ok" if db_ok else "error",
        "engine": "up" if engine_ok else "down",
        "terminal": {
            "ok": terminal_ok,
            # "local" = this process; "remote" = a separately hosted service.
            "mode": "remote" if terminal_link.is_remote() else "local",
            "host": terminal_host,
        },
        "storage": {
            "driver": "postgres" if IS_POSTGRES else "sqlite",
            "persistent": IS_POSTGRES,
            "warning": None if IS_POSTGRES else EPHEMERAL_DB_WARNING,
        },
    }


async def _terminal_health() -> tuple[bool, str]:
    """Is the terminal reachable? The gateway is healthy with a terminal on
    another host, but not one that is configured and refusing to answer."""
    from terminal import link as terminal_link
    from terminal.config import TERMINAL_SERVICE_URL

    if not terminal_link.is_remote():
        return True, "in-process"
    try:
        await terminal_link.current().snapshot()
        return True, TERMINAL_SERVICE_URL
    except Exception as err:
        log.warning("terminal service %s unreachable: %s", TERMINAL_SERVICE_URL, err)
        return False, TERMINAL_SERVICE_URL


@app.get("/health")
@app.get("/api/health")
async def health():
    return JSONResponse(await health_payload())


@app.head("/health")
async def health_head():
    return Response(status_code=200)


# --------------------------------------------------------------------------- #
# API routers (gateway mounted at both /v1 and /api/v1)
# --------------------------------------------------------------------------- #

from routers import (  # noqa: E402
    admin_client_keys,
    admin_compute,
    admin_keys,
    admin_limits,
    admin_mcp,
    admin_misc,
    admin_models,
    admin_providers,
    admin_routes,
    admin_routing,
    admin_storage,
    admin_terminal,
    admin_trading,
    agent_api,
    build_api,
    gateway,
)

admin_router = APIRouter(prefix="/api/admin")
admin_router.include_router(admin_providers.router, prefix="/providers", tags=["providers"])
admin_router.include_router(admin_models.router, prefix="/models", tags=["models"])
admin_router.include_router(admin_keys.router, prefix="/keys", tags=["keys"])
admin_router.include_router(admin_routes.router, prefix="/routes", tags=["routes"])
admin_router.include_router(admin_routing.router, tags=["routing"])
admin_router.include_router(admin_client_keys.router, prefix="/client-keys", tags=["client-keys"])
admin_router.include_router(admin_terminal.router, prefix="/terminal", tags=["terminal"])
admin_router.include_router(admin_compute.router, prefix="/compute", tags=["compute"])
admin_router.include_router(admin_storage.router, prefix="/storage", tags=["storage"])
admin_router.include_router(admin_mcp.router, prefix="/mcp", tags=["mcp"])
admin_router.include_router(admin_misc.router, tags=["misc"])
admin_router.include_router(admin_limits.router, tags=["limits"])
admin_router.include_router(admin_trading.router, prefix="/trading", tags=["trading"])

# Permanent cloud build config (Root@Build terminal) — providers, tool registry.
build_router = APIRouter(prefix="/api/build")
build_router.include_router(build_api.router, tags=["build"])

agent_router = APIRouter(prefix="/api/agent")
agent_router.include_router(agent_api.router, tags=["agent"])

app.include_router(gateway.router, prefix="/v1", tags=["gateway"])
app.include_router(gateway.router, prefix="/api/v1", include_in_schema=False, tags=["gateway"])
app.include_router(admin_router)
app.include_router(build_router)
app.include_router(agent_router)

# --------------------------------------------------------------------------- #
# Python UI (frontend module) — shell + tab fragments from `ui/`
# --------------------------------------------------------------------------- #

from ui.shell import shell_router  # noqa: E402
from ui.tabs import ui_router  # noqa: E402

app.include_router(shell_router)
app.include_router(ui_router)
class CachedStaticFiles(StaticFiles):
    """Static assets with a correct Cache-Control.

    Without a header a browser (and every CDN/proxy in front of Render) is free
    to revalidate or heuristically cache forever. Templates reference these
    files with a `?v=` query — that query is not forwarded to the directory, so
    the version token is the only invalidation: `must-revalidate` keeps a
    swapped asset from sticking for days, while `max-age=0` repaints a page
    that embeds a stale one only after an ETag check (StaticFiles still answers
    `304` from the request's validators).
    """

    async def get_response(self, path, scope):  # noqa: ANN001 - Starlette signature
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return response


app.mount("/static", CachedStaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    # requirement #1: bind 0.0.0.0:$PORT — PORT comes from the environment
    # (Render assigns it dynamically). No hardcoded platform port anywhere.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
