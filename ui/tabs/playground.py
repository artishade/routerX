"""Playground tab — send a chat completion through this gateway and watch it.

The whole point is that the request goes through the app's OWN gateway
(`POST /v1/chat/completions` via ui/api_client), so what the Playground shows
is exactly what an API client would get: the identity-spoofed `model` field
plus the honest `_nova` routing metadata (upstream model, provider, stage,
fallback, cache).

Routes:
  GET  /partials/tab/playground   frame + model picker options
  POST /ui/playground/send        HTMX form post → answer fragment
                                  (streaming is handled in the browser by
                                  static/app.js `initPlayground()`)

Wire format: the JSON we forward is snake_case (OpenAI shape); the fragment
context we pass to Jinja is snake_case too.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ui.api_client import ApiError, api
from ui.render import form_dict, html, render

router = APIRouter(tags=["ui:playground"])

# Gateway models are exposed ids; the picker renders every advertised id
# (the catalogue is curated, so this is a safety cap, not a pagination page).
MAX_PICKER_MODELS = 300

# Streaming metadata: `_chat_stream_response` (routers/gateway.py) emits the
# data chunks and a final `[DONE]`, but never the `_nova` block — only a
# non-streaming body carries it. So the streamed path cannot show provider /
# stage chips from the stream itself. It is answered by
# `GET /ui/playground/meta?request_id=<id>` instead, which reads this request's
# own RequestLog row. That row is written in the stream generator's `finally`
# and carries the same id only if the gateway is configured to forward it
# (see the module note in app.js); when it cannot be matched the answer simply
# shows no chips — never a wrong provider. It is best-effort by design.

_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL)


def _clean(text: Any) -> str:
    """Strip provider chain-of-thought wrappers (gateway parity)."""
    return _THINK_RE.sub("", str(text or "")).strip()


async def _model_options() -> list[dict[str, Any]]:
    """Exposed model ids from the gateway's own /v1/models listing.

    Degrades to an empty list (the template explains it) instead of failing the
    whole tab — the picker also accepts free text.
    """
    try:
        payload = await api.get("/v1/models")
    except Exception:  # noqa: BLE001 — picker is decoration, never fatal
        return []
    rows = payload.get("data") if isinstance(payload, dict) else None
    # (payload is a dict for both /v1/models shapes)
    out: list[dict[str, Any]] = []
    for m in rows or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        out.append({"id": m.get("id"), "owned_by": m.get("owned_by") or ""})
        if len(out) >= MAX_PICKER_MODELS:
            break
    return out


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _messages(system: str, user: str, history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for turn in history or []:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and turn.get("content"):
            msgs.append({"role": turn["role"], "content": str(turn["content"])})
    msgs.append({"role": "user", "content": user})
    return msgs


# --------------------------------------------------------------------------- #
# GET /partials/tab/playground
# --------------------------------------------------------------------------- #

@router.get("/partials/tab/playground")
async def playground_tab() -> HTMLResponse:
    return html(render("tabs/playground.html", models=await _model_options(), error=None, result=None))


# --------------------------------------------------------------------------- #
# POST /ui/playground/send — non-streaming answer fragment
# --------------------------------------------------------------------------- #

@router.post("/ui/playground/send")
async def playground_send(request: Request) -> HTMLResponse:
    form = await form_dict(request)
    # Echo the browser's correlation id back so an HTMX post can also be
    # matched to its RequestLog row without a second lookup pass.
    headers = {"x-playground-request-id": str(form.get("request_id") or "")[:64]}
    model = str(form.get("model") or "").strip() or "auto"
    system = str(form.get("system") or "").strip()
    user = str(form.get("message") or "").strip()
    temperature = _as_float(form.get("temperature"), 0.7, 0.0, 2.0)
    max_tokens = _as_int(form.get("max_tokens"), 1024, 1, 32768)

    if not user:
        return html(
            render("partials/playground_result.html", error="Type a message first.", user=None, result=None, model=model),
            headers=headers,
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": _messages(system, user, None),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    started = asyncio.get_event_loop().time()
    try:
        body = await api.post("/v1/chat/completions", json=payload)
    except ApiError as err:
        return html(
            render(
                "partials/playground_result.html",
                error=err.message or "The gateway rejected the request",
                status=err.status,
                user=user,
                result=None,
                model=model,
            ),
            headers=headers,
        )
    except Exception as err:  # noqa: BLE001 — inline error fragment, never a 500 page
        return html(
            render(
                "partials/playground_result.html",
                error=f"Gateway unreachable ({err.__class__.__name__})",
                status=0,
                user=user,
                result=None,
                model=model,
            ),
            headers=headers,
        )

    latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    if not isinstance(body, dict):
        return html(
            render("partials/playground_result.html", error="The gateway returned an unexpected body.",
                   status=0, user=user, result=None, model=model),
            headers=headers,
        )

    choice = ((body.get("choices") or [{}])[0] or {})
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    nova = body.get("_nova") if isinstance(body.get("_nova"), dict) else {}

    result = {
        "content": _clean(message.get("content")),
        "finish_reason": choice.get("finish_reason") or "stop",
        "model": body.get("model") or model,
        "nova": nova,
        "latency_ms": latency_ms,
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "streamed": False,
    }
    return html(render("partials/playground_result.html", error=None, user=user, result=result, model=model),
                headers=headers)


# --------------------------------------------------------------------------- #
# GET /ui/playground/meta — routing chips for ONE streamed request
#
# The stream itself carries no `_nova`, so static/app.js asks here after the
# stream ends. Matching is by `x-request-id` (correlation id), never by a
# timestamp window like `/ui/console/chat-meta`, so a miss returns "unknown"
# instead of another client's reply.
#
# The RequestLog row is written in the stream generator's `finally`, i.e. after
# the last byte reaches the browser, so the client polls a few times.
# --------------------------------------------------------------------------- #

@router.get("/ui/playground/meta")
async def playground_meta(request_id: str = "") -> JSONResponse:
    rid = (request_id or "").strip()[:32]
    if not rid:
        return JSONResponse({"ok": False})
    try:
        logs = await api.get("/api/admin/logs", params={"limit": 50})
    except (ApiError, Exception):  # noqa: BLE001 — chips are decoration
        return JSONResponse({"ok": False})
    for entry in logs or []:
        if not isinstance(entry, dict):
            continue
        # Accept both spellings the admin log may carry for the correlation id.
        entry_rid = str(entry.get("request_id") or entry.get("requestId") or "")
        if entry_rid != rid:
            continue
        requested = str(entry.get("model") or "")
        upstream = str(entry.get("upstream_model") or requested)
        return JSONResponse({
            "ok": True,
            "nova": {
                "provider": entry.get("provider_name") or "",
                "upstream_model": upstream,
                "fallback": bool(requested and upstream and upstream != requested),
                # RequestLog has no pipeline stage; the gateway's stage is
                # reconstructible only from `via`, which is honest enough.
                "stage": None,
                "cached": str(entry.get("via") or "") == "cache",
                "spoofed": bool(entry.get("spoofed")),
                "latency_ms": entry.get("latency_ms") or 0,
                "ttft_ms": entry.get("ttft_ms") or 0,
                "tokens_in": entry.get("tokens_in") or 0,
                "tokens_out": entry.get("tokens_out") or 0,
            },
        })
    return JSONResponse({"ok": False})
