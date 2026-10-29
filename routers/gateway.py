"""OpenAI-compatible gateway — /v1/* (also mounted under /api/v1/*).

Endpoints (requirement #3):
  GET  /v1/models               list models (DB) — ?discover=1 live discovery
  POST /v1/chat/completions     streaming (SSE) + non-streaming, full fallback pipeline
  POST /v1/completions          legacy completions (prompt → chat pipeline)
  POST /v1/messages             Anthropic-format adapter (routers/anthropic_adapter.py)
  POST /v1/messages/count_tokens  Claude Code pre-flight token count
  POST /v1/embeddings           embeddings via upstream providers
  POST /v1/responses            OpenAI Responses API adapter (routers/openai_responses.py)

Every route authenticates through `nova.auth.gateway_auth` (client keys, RPM,
allowedModels, daily token budget) and books usage through
`nova.auth.attribute_client` - one writer for ClientKey counters.

Routing pipeline:
  Stage 1     — direct upstream (requested model, enabled key)
  Stage 2..N  — fallback chain: the ModelRoute `fallbacks` list first (publicId
                match, else '*'), then any richer declarative chains registered
                in `routing_fallback_chains`, then the auto stand-in pool
                (every other healthy, enabled provider) while the route's `auto`
                flag is on — the default
  Final stage — RouterX Free engine (bundled free-model catalogue, no key required)

The order of those stages is produced by `nova.routing` (the routing brain:
above): policy engine → exposure lists → tag routing → per-(provider,
model) lockouts → selection strategy. Every exclusion is recorded so `_nova`
can explain why a request did not use the provider the operator expected.

The response `model` field is ALWAYS the requested id (identity spoofing);
`_nova` metadata exposes what actually served the request. Every request is
logged to RequestLog.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from nova import engine as nova_engine
from nova import catalog as nova_catalog
from nova import routing as nova_routing
from nova import router_settings
from nova import auth as nova_auth
from nova import cache as nova_cache
from nova import profiles as nova_profiles
from nova import ratelimit as nova_ratelimit
from nova import session as nova_session
from nova.discovery import aggregate_discovery, discover_provider_models
from nova.models import ClientKey, Model as ModelRow, ModelRoute, Provider, ProviderKey, RequestLog
from nova.provider_transport import (
    anthropic_headers, anthropic_to_openai, anthropic_url, openai_to_anthropic,
    extract_forward_headers, forward_response_headers, response_json_dict,
)

log = logging.getLogger("nova.gateway")


class HTTPError(Exception):
    """A failed request stage with the HTTP status the client should receive."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.message = detail

router = APIRouter()

UPSTREAM_CONNECT_TIMEOUT = float(os.environ.get("NOVA_CONNECT_TIMEOUT", "5.0"))
# Read timeout = max silence BETWEEN bytes from upstream. Claude/long-thinking runs
# can legally sit silent for minutes (thinking blocks, cold queue) — 60s caused
# ReadTimeout mid-stream → "request failed" in Claude Code. Env-tunable.
UPSTREAM_READ_TIMEOUT = float(os.environ.get("NOVA_UPSTREAM_READ_TIMEOUT", "600.0"))
NONSTREAM_READ_TIMEOUT = float(os.environ.get("NOVA_NONSTREAM_READ_TIMEOUT", "300.0"))
UPSTREAM_TIMEOUT = httpx.Timeout(connect=UPSTREAM_CONNECT_TIMEOUT, read=UPSTREAM_READ_TIMEOUT, write=120.0, pool=30.0)

# Shared client — connection pooling / keep-alive across requests. A fresh
# AsyncClient per request pays a full TCP+TLS handshake to the upstream every
# call (hundreds of ms each); a pooled client reuses warm connections.
UPSTREAM_CLIENT = httpx.AsyncClient(
    timeout=UPSTREAM_TIMEOUT,
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=60.0),
)
NONSTREAM_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=UPSTREAM_CONNECT_TIMEOUT, read=NONSTREAM_READ_TIMEOUT, write=120.0, pool=30.0),
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=60.0),
)

# Provider-key cache — avoids a DB roundtrip per upstream attempt.
_KEY_CACHE: dict[int, tuple[float, dict | None]] = {}
_KEY_CACHE_TTL = 30.0

# Lockouts are flushed to SystemConfig at most once a minute.
_LOCKOUT_FLUSH_TS = 0.0

# Auto stand-ins: once a route's explicit chain is exhausted (and the route's
# `auto` flag is on — the default), the pipeline appends other healthy, enabled
# providers' models. That is what makes a dead provider fall over onto the rest
# of the pool the operator configured instead of jumping straight to the free
# engine. The dashboard preview calls the same helper, so they never disagree.
AUTO_STANDIN_LIMIT = int(os.environ.get("NOVA_AUTO_STANDINS", "6"))

# Sticky sessions / named profiles: per-request context. Thread-local because
# FastAPI runs sync dependencies (`gateway_auth`) in the threadpool and the
# endpoint that follows may run on that same thread; a streaming generator must
# re-enter with `enter_request()` because it runs on the event loop.
_REQUEST_CTX = threading.local()


def _ctx() -> dict[str, Any]:
    ctx = getattr(_REQUEST_CTX, "value", None)
    if ctx is None:
        ctx = {}
        _REQUEST_CTX.value = ctx
    return ctx


def enter_request(*, session_id: str = "", profile: str = "",
                  client_key_id: int | None = None) -> dict[str, Any]:
    """Start a request context (streams call this from inside the generator)."""
    ctx = {"session_id": session_id, "profile": profile, "client_key_id": client_key_id,
           "handoff": None, "ttft_ms": 0, "via": "upstream", "served_model": "",
           "provider_id": None, "upstream_model": ""}
    _REQUEST_CTX.value = ctx
    return ctx


def current_session_id() -> str:
    return str(_ctx().get("session_id") or "")


def current_handoff() -> dict | None:
    return _ctx().get("handoff")


def _note_handoff(requested: str, served_model: str, from_model: str, reason: str) -> dict | None:
    """Record a sticky-session switch: `{from, to, reason}` for `_nova.handoff`."""
    sid = current_session_id()
    if not sid or not from_model or from_model == served_model:
        return None
    note = nova_session.handoff(sid, from_model, served_model, reason)
    _ctx()["handoff"] = note
    return note

# SSE keep-alive: when an upstream is silent (cold start, long thinking, queue)
# a proxy with a short idle timeout drops the connection and the client reports
# a failure roughly 10-15s in. A comment line / ping every few seconds keeps the
# stream demonstrably alive end to end. Env-tunable.
SSE_HEARTBEAT_S = float(os.environ.get("NOVA_SSE_HEARTBEAT", "10.0"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str | None) -> str | None:
    """Remove  thinking...</think> blocks (including unclosed trailing  thinking).

    Coerces non-string upstream content (some providers return a list of text
    parts) so callers never crash on an unusual but valid payload shape.
    """
    if not text:
        return text
    if isinstance(text, list):
        text = "\n".join(
            str(part.get("text", "")) for part in text
            if isinstance(part, dict) and "text" in part
        )
    elif not isinstance(text, str):
        text = str(text)
    # Remove complete blocks
    cleaned = THINK_BLOCK_RE.sub("", text)
    # If an opening <think> remains without closing, drop it and everything after
    # (handles truncated stream or unclosed reasoning)
    if re.search(r"<think>", cleaned, re.IGNORECASE):
        cleaned = THINK_OPEN_RE.sub("", cleaned)
        # also handle leftover case like "</think>" stray
        cleaned = re.sub(r"</think>", "", cleaned, flags=re.IGNORECASE)
    # Also strip common leaked tool-call markdown that lives INSIDE think leak
    # e.g. "</think>```bash\nls -la ..."  - if entire remaining text is just that, drop
    cleaned = cleaned.strip()
    return cleaned


class ThinkStreamFilter:
    """Stateful filter for SSE streaming - drops text inside <think>...</think> even when split across chunks."""
    def __init__(self):
        self.in_think = False
        self.buf = ""  # holds partial tag across chunks
    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        combined = self.buf + chunk
        self.buf = ""
        out_parts = []
        i = 0
        low = combined.lower()
        while i < len(combined):
            if not self.in_think:
                idx = low.find("<think>", i)
                if idx == -1:
                    # check for partial opening tag at tail
                    tail = combined[i:]
                    # hold back up to 6 chars that could be start of tag
                    for k in range(6,0,-1):
                        if len(tail) >= k and tail[-k:].lower() == "<think>"[:k]:
                            # check that tail actually ends with that prefix and not already emitted
                            out_parts.append(tail[:-k])
                            self.buf = tail[-k:]
                            return "".join(out_parts)
                    out_parts.append(tail)
                    break
                else:
                    out_parts.append(combined[i:idx])
                    self.in_think = True
                    i = idx + 7
                    low = combined.lower()  # keep in sync
            else:
                idx = low.find("</think>", i)
                if idx == -1:
                    # partial closing tag?
                    tail = combined[i:]
                    for k in range(8,0,-1):
                        if len(tail) >= k and tail[-k:].lower() == "</think>"[:k]:
                            # remain in think, buffer partial
                            self.buf = tail[-k:]
                            return "".join(out_parts)
                    # entire tail is inside think -> drop it
                    break
                else:
                    self.in_think = False
                    i = idx + 8
        return "".join(out_parts)
    def flush(self) -> str:
        # if we were holding a partial tag that never completed and not in think, emit it
        # but if in_think, drop everything
        if self.in_think:
            self.buf = ""
            return ""
        b = self.buf
        self.buf = ""
        # if buffered partial looks like a think tag start, drop it (was inside leak)
        if b.lower().startswith("<"):
            return ""
        return b


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_messages(raw: list[dict]) -> list[dict]:
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        # preserve tool role and assistant tool_calls
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or m.get("toolCallId") or "",
                "content": m.get("content") if isinstance(m.get("content"), str) else str(m.get("content") or ""),
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            # preserve full assistant tool_calls verbatim
            entry = {"role": "assistant", "content": m.get("content")}
            # keep tool_calls as-is (OpenAI format)
            entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            out.append(entry)
            continue
        # `developer` is OpenAI's newer spelling of the system role; mapping it
        # to `user` would silently demote Codex/OpenAI-SDK instructions.
        if role == "developer":
            role = "system"
        role = role if role in ("system", "assistant", "user") else "user"
        content = m.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(p.get("text", "")) for p in content
                if isinstance(p, dict) and "text" in p
            ).strip("\n")
        else:
            text = str(content or "")
        entry = {"role": role, "content": text}
        # Preserve tool_calls if present on any role (some providers send it on user)
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            entry["name"] = m["name"]
        out.append(entry)
    return out


def extract_upstream_content(payload: Any) -> str | None:
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or not choices:
            return None
        msg = choices[0]["message"]
        if not isinstance(msg, dict):
            return None
        # tool_calls response has no content but has tool_calls -> treat as success
        if msg.get("tool_calls"):
            return "__tool_calls__"  # sentinel so caller knows it is tool call
        content = msg.get("content")
    except (KeyError, TypeError, IndexError, AttributeError):
        return None
    if content is None:
        return None
    if isinstance(content, str):
        return strip_think_blocks(content)
    if isinstance(content, list):
        joined = "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict) and "text" in p
        ).strip("\n")
        return strip_think_blocks(joined) or None
    return strip_think_blocks(str(content)) if content else None


def extract_upstream_message(payload: Any) -> dict | None:
    """Extract full assistant message (content + tool_calls) for passthrough."""
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or not choices:
            return None
        msg = choices[0]["message"]
        if not isinstance(msg, dict):
            return None
        return msg
    except (KeyError, TypeError, IndexError):
        return None


PASSTHROUGH_FIELDS = ["tools", "tool_choice", "response_format", "max_tokens", "max_completion_tokens", "top_p", "top_k", "frequency_penalty", "presence_penalty", "stop", "seed", "n", "logit_bias", "user", "parallel_tool_calls", "reasoning_effort", "verbosity"]


def build_upstream_body(original_body: dict, model_id: str, messages: list[dict], stream: bool = False) -> dict:
    body: dict = {"model": model_id, "messages": messages}
    if stream:
        body["stream"] = True
    # passthrough common fields
    for field in PASSTHROUGH_FIELDS:
        if field in original_body and original_body[field] is not None:
            body[field] = original_body[field]
    # temperature / top_p special handling (allow 0)
    for k in ["temperature", "top_p"]:
        if k in original_body and isinstance(original_body[k], (int, float)):
            body[k] = original_body[k]
    # stream_options
    if "stream_options" in original_body:
        body["stream_options"] = original_body["stream_options"]
    return body


def find_model(db: Session, id_or_exposed: str) -> ModelRow | None:
    return db.scalars(
        select(ModelRow)
        .where((ModelRow.exposedId == id_or_exposed) | (ModelRow.modelId == id_or_exposed))
        .options(joinedload(ModelRow.provider))
        # Prefer an enabled row when the same model id is exposed by several
        # providers; otherwise a disabled duplicate would shadow the live one.
        .order_by(ModelRow.enabled.desc(), ModelRow.id)
    ).first()


def resolve_chain(db: Session, requested: str) -> list[str]:
    route = db.scalars(
        select(ModelRoute).where(ModelRoute.publicId == requested, ModelRoute.enabled.is_(True))
    ).first()
    if route is None:
        route = db.scalars(
            select(ModelRoute).where(ModelRoute.publicId == "*", ModelRoute.enabled.is_(True))
        ).first()
    if route is None:
        return []
    try:
        parsed = json.loads(route.fallbacks)
        return [f for f in parsed if isinstance(f, str)] if isinstance(parsed, list) else []
    except Exception:
        return []


def write_log(db: Session, **p) -> None:
    """One RequestLog row per gateway request. Never raises."""
    key_id = p.get("client_key_id")
    if key_id is None:
        key_id = _ctx().get("client_key_id")
    try:
        db.add(RequestLog(
            model=p.get("model", ""), upstreamModel=p.get("upstream_model", ""),
            providerName=p.get("provider_name", ""), endpoint=p.get("endpoint", "/v1/chat/completions"),
            status=p.get("status", 200), latencyMs=p.get("latency_ms", 0),
            tokensIn=p.get("tokens_in", 0), tokensOut=p.get("tokens_out", 0),
            via=p.get("via", ""), spoofed=p.get("spoofed", False), error=p.get("error", ""),
            clientKeyId=key_id, clientName=p.get("client_name", ""),
            ttftMs=p.get("ttft_ms", 0),
        ))
        db.commit()
    except Exception:  # logging must never break the response
        db.rollback()


def record_served(db: Session, requested: str, result: dict, messages: list[dict] | None = None,
                  tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Book a served upstream result in the usage windows + sticky-session pin.

    Called from every success path with the identity keys `attempt_upstream` (an
    `upstream_key_id`) or `pipeline_stream` (a `key_id`) put on the result, so
    per-(provider, model) caps get real numbers without the endpoint having to
    know where they came from.
    """
    provider_id = result.get("provider_id")
    model_id = str(result.get("upstream_model") or "")
    if provider_id is None or not model_id:
        return
    key_id = result.get("upstream_key_id") or result.get("key_id")
    try:
        nova_ratelimit.record_usage(db, provider_id, model_id, key_id, tokens_in, tokens_out)
    except Exception:
        pass
    sid = current_session_id()
    if sid:
        nova_session.pin(sid, str(result.get("upstream_exposed_id") or requested),
                         str(result.get("provider_name") or ""), model_id)


async def auth_refusal(err: nova_auth.AuthError, requested: str = "",
                       endpoint: str = "/v1/chat/completions") -> JSONResponse:
    """The gateway's answer to an `AuthError` (401/403/429 + Retry-After).

    Every refused gateway request still writes its RequestLog row.
    """
    from nova.database import SessionLocal

    try:
        with SessionLocal() as db:
            write_log(db, model=requested, provider_name="", endpoint=endpoint,
                      status=err.status, latency_ms=0, via="auth",
                      error=f"{err.code}: {err.message}")
    except Exception:
        pass
    return JSONResponse(err.to_payload(), status_code=err.status, headers=err.headers())


def enforce_client(principal, requested: str) -> nova_auth.AuthError | None:
    """Per-model rules for the resolved client key (403 / daily budget), or None.

    `gateway_auth` already spent the RPM slot; this adds the rules that need the
    model name, which only exists after the body is parsed.
    """
    key = getattr(principal, "key", None) if principal is not None else None
    if key is None:
        return None
    allowed = nova_auth.parse_allowed_models(key.allowedModels)
    if not nova_auth.model_allowed(allowed, requested):
        return nova_auth.AuthError(
            403, nova_auth.CODE_MODEL_FORBIDDEN,
            f"This API key is not allowed to use model '{requested}'.")
    tpd = int(key.tpdLimit or 0)
    if tpd > 0 and (int(key.tokensIn or 0) + int(key.tokensOut or 0)) >= tpd:
        return nova_auth.AuthError(
            429, nova_auth.CODE_TPD_EXCEEDED,
            f"Daily token budget exhausted for this API key ({tpd} tokens/day).",
            retry_after=nova_auth.seconds_until_utc_midnight())
    return None


def attribute_client(db: Session, principal, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Persist usage for the request's client key (one writer: nova.auth)."""
    key = getattr(principal, "key", None) if principal is not None else None
    if key is not None:
        nova_auth.update_usage(db, key.id, tokens_in, tokens_out)


def nova_meta(upstream_model: str, provider_name: str, stage: int, requested: str,
              fallback: bool, routing_block: dict | None = None, *,
              cached: bool = False, handoff: dict | None = None,
              ttft_ms: int | None = None) -> dict:
    """The `_nova` block. Extra keys are additive; existing ones never move."""
    meta = {
        "upstream_model": upstream_model,
        "provider": provider_name,
        "fallback": fallback,
        "hedged": False,
        "cached": cached,
        "spoofed": requested != upstream_model,
        "stage": stage,
    }
    if routing_block:
        meta["routing"] = routing_block
    if handoff:
        meta["handoff"] = handoff
    if ttft_ms:
        meta["ttft_ms"] = int(ttft_ms)
    return meta


def build_headers() -> dict:
    return {"x-content-type-options": "nosniff"}


class UpstreamFailure(Exception):
    """One failed upstream stage.

    `status` is the upstream HTTP status (0 for transport faults) and
    `retry_after` the provider's `Retry-After` when it sent one; together they
    drive the key cooldown and the "is another key worth trying" decision.
    """

    def __init__(self, detail: str, status: int = 0, retry_after: str | int | None = None,
                 provider_id: int | None = None, key_id: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.retry_after = retry_after
        self.provider_id = provider_id
        self.key_id = key_id


# --------------------------------------------------------------------------- #
# Non-streaming upstream attempt (parity with attemptUpstream)
# --------------------------------------------------------------------------- #

async def attempt_upstream(row: ModelRow, messages: list[dict], temperature: float | None,
                           original_body: dict | None = None,
                           forward_headers: dict[str, str] | None = None,
                           key_info: dict | None = None,
                           block: set[int] | None = None) -> dict | None:
    """One non-streaming upstream attempt on ONE key.

    `key_info` is the key chosen by the caller (so a key retry is explicit);
    without it the default selection runs here. `block` carries key ids already
    tried. A 429/401/5xx records a key cooldown before returning `None`; a
    success clears it and books the key's request counters. The result carries
    `upstream_key_id` so usage can be attributed to the key that paid for it.
    """
    provider = row.provider
    if not provider or not provider.enabled or provider.kind in ("builtin", "gemini", "exchange"):
        return None
    base = (provider.baseUrl or "").rstrip("/")
    if not base or base.startswith("internal://"):
        return None

    if original_body is not None:
        body = build_upstream_body(original_body, row.modelId, messages, stream=False)
    else:
        body = {"model": row.modelId, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature

    if key_info is None:
        key_info = await asyncio.to_thread(select_upstream_key_sync, provider.id, block)
    if not key_info:
        return None
    key_id = key_info.get("keyId")

    try:
        native = provider.kind == "anthropic"
        res = await NONSTREAM_CLIENT.post(
            anthropic_url(base) if native else f"{base}/chat/completions",
            headers=({**anthropic_headers(key_info["apiKey"], forward=forward_headers),
                      "Accept": "application/json"} if native else
                     {"Content-Type": "application/json", "Accept": "application/json",
                      "Authorization": f"Bearer {key_info['apiKey']}"}),
            json=openai_to_anthropic(row.modelId, messages, body) if native else body,
        )
        if res.status_code < 200 or res.status_code >= 300:
            log.info("upstream %s/%s returned HTTP %s", provider.id, row.modelId, res.status_code)
            await asyncio.to_thread(cooldown_after_failure, provider.id, key_id, res.status_code,
                                    res.headers.get("retry-after"),
                                    f"HTTP {res.status_code} on {row.modelId}")
            return None
        if "text/event-stream" in (res.headers.get("content-type") or "").lower():
            # Some OpenAI-compatible proxies stream even when `stream` is absent.
            # Parsing that as JSON used to raise ValueError -> HTTP 500.
            log.warning("upstream %s/%s streamed a 2xx to a non-stream request - next stage",
                        provider.id, row.modelId)
            return None
        payload = response_json_dict(res)
        if payload is None:
            log.warning("upstream %s/%s returned a non-JSON 2xx body - trying the next stage",
                        provider.id, row.modelId)
            return None
        if native:
            normalized = anthropic_to_openai(payload)
            if not isinstance(normalized, dict):
                return None
            await asyncio.to_thread(note_key_success, key_id)
            return {**normalized, "upstream_model": row.modelId,
                    "upstream_exposed_id": row.exposedId, "provider_name": provider.name,
                    "provider_id": provider.id, "via": "upstream-anthropic",
                    "upstream_key_id": key_id,
                    "upstream_headers": forward_response_headers(res)}
        content = extract_upstream_content(payload)
        if content in (None, ""):
            # Empty 2xx completions are a provider symptom, not an answer: let
            # the next stage serve the agent instead of returning a blank turn.
            return None
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        upstream_msg = extract_upstream_message(payload)
        await asyncio.to_thread(note_key_success, key_id)
        return {
            "content": content if content != "__tool_calls__" else (upstream_msg.get("content") if upstream_msg else None),
            "upstream_model": row.modelId,
            "upstream_exposed_id": row.exposedId,
            "provider_name": provider.name,
            "provider_id": provider.id,
            "via": "upstream",
            "upstream_key_id": key_id,
            "upstream_message": upstream_msg,
            "tool_calls": upstream_msg.get("tool_calls") if upstream_msg else None,
            "finish_reason": (payload.get("choices") or [{}])[0].get("finish_reason"),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
        }
    except httpx.HTTPError as err:
        log.info("upstream %s/%s transport failed: %s", provider.id, row.modelId, err)
        return None
    except Exception as err:  # noqa: BLE001 - one bad provider must never 500 the request
        log.warning("upstream %s/%s raised %s: %s", provider.id, row.modelId,
                    err.__class__.__name__, err)
        return None


async def attempt_upstream_with_key_retry(row: ModelRow, messages: list[dict], temperature: float | None,
                                          original_body: dict | None = None,
                                          forward_headers: dict[str, str] | None = None) -> dict | None:
    """`attempt_upstream`, retried ONCE on the provider's next key.

    A per-key failure (quota exhausted on that key, token revoked, key-specific
    5xx) should not cost the whole model transition: the same model is tried on
    one other key first, then the chain moves on as usual.
    """
    provider_id = row.provider.id if row.provider is not None else None
    if provider_id is None:
        return await attempt_upstream(row, messages, temperature, original_body, forward_headers)
    block: set[int] = set()
    for _attempt in range(2):
        try:
            key_info = await asyncio.to_thread(select_upstream_key_sync, provider_id, set(block))
        except Exception:
            key_info = None
        if key_info is None:
            return None
        result = await attempt_upstream(row, messages, temperature, original_body, forward_headers,
                                        key_info=key_info)
        if result is not None:
            return result
        # Exclude the failed key; pick_key then returns None once no other key is
        # usable, instead of falling back onto a key already proven bad here.
        block.add(int(key_info.get("keyId") or 0))
    return None


class UpstreamStream(tuple):
    """`(iterator, provider_name, upstream_model[, key_id])` from one open stream.

    Unpacks as the historical 3-tuple (the stream itself, provider, model) so
    existing call sites keep working, while `.key_id` carries the upstream key
    that opened it for cooldown/usage bookkeeping.
    """

    def __new__(cls, iterator, provider_name: str, upstream_model: str, key_id: int | None = None):
        obj = super().__new__(cls, (iterator, provider_name, upstream_model, key_id))
        obj.iterator = iterator
        obj.provider_name = provider_name
        obj.upstream_model = upstream_model
        obj.key_id = key_id
        return obj

    def __iter__(self):
        return iter((self.iterator, self.provider_name, self.upstream_model))

    def __len__(self):
        return 3


def _key_rows(session: Session, provider_id: int) -> list[ProviderKey]:
    """Enabled keys for a provider in selection order (weight desc, id asc)."""
    return list(session.scalars(
        select(ProviderKey)
        .where(ProviderKey.providerId == provider_id, ProviderKey.enabled.is_(True))
        .order_by(ProviderKey.weight.desc(), ProviderKey.id)
    ).all())


def _key_columns(session: Session, provider_id: int):
    """`(apiKey, id)` for each enabled key, tolerant of partial row objects.

    Selection only needs the secret plus a stable id. Reading them as columns
    keeps every caller (including a session whose rows were built without the
    newer bookkeeping attributes) working unchanged.
    """
    return session.execute(
        select(ProviderKey.apiKey, ProviderKey.id, ProviderKey.weight, ProviderKey.cooldownUntil)
        .where(ProviderKey.providerId == provider_id, ProviderKey.enabled.is_(True))
        .order_by(ProviderKey.weight.desc(), ProviderKey.id)
    ).all()


def _key_info(key: ProviderKey, provider_id: int) -> dict:
    return {"apiKey": key.apiKey, "keyId": getattr(key, "id", None),
            "providerId": provider_id, "label": getattr(key, "label", "")}


def select_upstream_key_sync(provider_id: int, blocked: set[int] | None = None,
                             use_stale_cache: bool = True):
    """Pick the upstream key to use for one attempt.

    Only the *choice* is cached (30s); the cooling state is re-read every call,
    because a cooldown set by a failed request must skip that key immediately -
    a cached "key #1" used to put a cooling key back on the hot path.

    Selection policy (see `nova.ratelimit.pick_key`): the highest-weight key
    that is not cooling down. A cooling key is used **only** when every enabled
    key for the provider is cooling, and then the one expiring soonest: still
    falling through to a possibly-working key beats reporting no provider at
    all. `blocked` carries key ids already tried for this request, so a retry
    moves to the next key instead of repeating the failed one.

    When the database cannot be reached, the last known key is returned
    (`use_stale_cache`) so a transient DB hiccup does not take the provider
    offline mid-stream.
    """
    from nova.database import SessionLocal

    cached = _KEY_CACHE.get(provider_id)
    fresh = cached is not None and time.time() - cached[0] < _KEY_CACHE_TTL
    try:
        with SessionLocal() as s:
            rows = _key_rows(s, provider_id)
            usable = [k for k in rows if not blocked or k.id not in blocked]
            key, _cooling_fallback = nova_ratelimit.pick_key(usable)
            if key is None:
                return None
            info = _key_info(key, provider_id)
            _KEY_CACHE[provider_id] = (time.time(), info)
            return info
    except SQLAlchemyError:
        log.warning("key lookup for provider %s failed - using the last known key", provider_id)
        if use_stale_cache and cached is not None:
            return cached[1]
        raise


def provider_keys_sync(provider_id: int) -> list[dict]:
    """Every enabled key for a provider, in selection order (no caching).

    Used by the same-model retry: when a key fails the attempt repeats on the
    next key before the chain moves on to another model.
    """
    from nova.database import SessionLocal

    with SessionLocal() as s:
        return [_key_info(k, provider_id) for k in _key_rows(s, provider_id)]


def cooldown_after_failure(provider_id: int | None, key_id: int | None, status: int,
                           retry_after=None, error: str = "") -> float:
    """Persist a key cooldown after a 429/401/5xx (0 for other statuses)."""
    if key_id is None:
        return 0.0
    from nova.database import SessionLocal

    try:
        with SessionLocal() as s:
            row = s.get(ProviderKey, int(key_id))
            err_count = int(row.errCount or 0) if row is not None else 0
            seconds = nova_ratelimit.cooldown_seconds(status, retry_after, err_count)
            if seconds > 0:
                nova_ratelimit.set_cooldown(s, key_id, seconds, error or f"HTTP {status}")
            return seconds
    except Exception:  # cooldown bookkeeping must never fail a request
        return 0.0


def note_key_success(key_id: int | None) -> None:
    """Clear a key's cooldown and bump its request counters after a success."""
    if key_id is None:
        return
    from nova.database import SessionLocal

    try:
        with SessionLocal() as s:
            nova_ratelimit.clear_cooldown(s, key_id)
    except Exception:
        pass


def _retry_worthy(status: int) -> bool:
    """Is another key worth a try for this upstream status?

    Only faults that can be key-specific qualify: auth (401/403), quota (429)
    and server-side errors (5xx). A 400/404 is the same on every key.
    """
    return status in (401, 403, 429) or 500 <= int(status or 0) < 600


def keys_to_try(provider_id: int | None, first_key_id: int | None) -> list[int | None]:
    """Key ids to attempt for one (provider, model): the first, then one retry.

    Retrying the same model on the next key recovers a per-key failure (quota,
    revoked token) without spending a whole model transition on it. Exactly one
    extra attempt is made, and only when another enabled key exists.
    """
    if provider_id is None:
        return [None]
    try:
        others = [k["keyId"] for k in provider_keys_sync(provider_id) if k["keyId"] != first_key_id]
    except Exception:
        others = []
    return [first_key_id] + (others[:1] if others else [])


# --------------------------------------------------------------------------- #
# Streaming upstream attempt (SSE relay)
# --------------------------------------------------------------------------- #

async def attempt_upstream_stream(row: ModelRow, messages: list[dict], temperature: float | None,
                                  original_body: dict | None = None,
                                  forward_headers: dict[str, str] | None = None,
                                  block: set[int] | None = None,
                                  key_info: dict | None = None):
    """Open an SSE stream to the upstream provider.

    Returns `(stream_iterator, provider_name, upstream_model, key_id)` or raises
    `UpstreamFailure` (carrying the status / `Retry-After` / key id) before
    anything is committed, so the caller can cool the key down, retry the same
    model on the next key, or fall through to the next stage.
    """
    provider = row.provider
    if not provider or not provider.enabled or provider.kind in ("builtin", "gemini", "exchange"):
        raise UpstreamFailure("provider not streamable", provider_id=provider.id if provider else None)
    if key_info is None:
        try:
            key_info = await asyncio.to_thread(select_upstream_key_sync, provider.id, block)
        except SQLAlchemyError as err:
            raise UpstreamFailure(f"key lookup failed: {err.__class__.__name__}",
                                  provider_id=provider.id) from err
    if key_info is None:
        raise UpstreamFailure("no enabled key", provider_id=provider.id)
    key_id = key_info.get("keyId")
    base = (provider.baseUrl or "").rstrip("/")
    if not base or base.startswith("internal://"):
        raise UpstreamFailure("no base url", provider_id=provider.id, key_id=key_id)

    body = build_upstream_body(original_body, row.modelId, messages, stream=True) if original_body is not None else {"model": row.modelId, "messages": messages, "stream": True, **({"temperature": temperature} if temperature is not None else {})}
    native = provider.kind == "anthropic"
    client = UPSTREAM_CLIENT
    try:
        req = client.build_request(
            "POST", anthropic_url(base) if native else f"{base}/chat/completions",
            headers=({**anthropic_headers(key_info["apiKey"], forward=forward_headers),
                      "Accept": "text/event-stream"} if native else
                     {"Content-Type": "application/json", "Accept": "text/event-stream",
                      "Authorization": f"Bearer {key_info['apiKey']}"}),
            json=openai_to_anthropic(row.modelId, messages, body, stream=True) if native else body,
        )
        res = await client.send(req, stream=True)
    except httpx.HTTPError as err:
        raise UpstreamFailure(f"connect failed: {err}", provider_id=provider.id, key_id=key_id) from err
    except Exception as err:  # noqa: BLE001 — never let provider setup crash the stream
        raise UpstreamFailure(f"request setup failed: {err.__class__.__name__}",
                              provider_id=provider.id, key_id=key_id) from err

    if res.status_code < 200 or res.status_code >= 300:
        retry_after = res.headers.get("retry-after")
        await res.aclose()
        raise UpstreamFailure(f"upstream returned HTTP {res.status_code}", status=res.status_code,
                              retry_after=retry_after, provider_id=provider.id, key_id=key_id)

    async def relay() -> AsyncIterator[dict]:
        tool_indices: dict[int, int] = {}
        try:
            async for line in res.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if native:
                    event = chunk.get("type")
                    if event == "content_block_start":
                        block = chunk.get("content_block") if isinstance(chunk.get("content_block"), dict) else {}
                        if block.get("type") == "tool_use":
                            index = len(tool_indices)
                            upstream_index = chunk.get("index", index)
                            if not isinstance(upstream_index, (int, str)):
                                upstream_index = index
                            tool_indices[upstream_index] = index
                            yield {"text": "", "finish_reason": None, "tool_calls": [{"index": index,
                                "id": block.get("id"), "type": "function", "function": {"name": block.get("name"), "arguments": ""}}]}
                    elif event == "content_block_delta":
                        delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                        idx = chunk.get("index")
                        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and delta["text"]:
                            yield {"text": delta["text"], "finish_reason": None, "tool_calls": None}
                        elif (delta.get("type") == "input_json_delta"
                              and isinstance(idx, (int, str)) and idx in tool_indices):
                            yield {"text": "", "finish_reason": None, "tool_calls": [{"index": tool_indices[idx],
                                 "function": {"arguments": delta.get("partial_json") or ""}}]}
                    elif event == "message_delta":
                        delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                        reason = delta.get("stop_reason")
                        if isinstance(reason, str) and reason:
                            yield {"text": "", "finish_reason": {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(reason, reason), "tool_calls": None}
                    continue
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    continue
                delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
                text = delta.get("content")
                if isinstance(text, list):
                    text = "\n".join(
                        str(part.get("text", "")) for part in text
                        if isinstance(part, dict) and "text" in part
                    )
                elif text is not None and not isinstance(text, str):
                    text = str(text)
                finish = choices[0].get("finish_reason")
                tool_calls_delta = delta.get("tool_calls")
                if text or finish or tool_calls_delta:
                    yield {"text": text or "", "finish_reason": finish, "tool_calls": tool_calls_delta, "raw_delta": delta, "raw_choice": choices[0]}
        finally:
            try:
                await res.aclose()
            except Exception:
                pass
            # client is the shared UPSTREAM_CLIENT — never close it, only the
            # per-response stream; the pooled connection returns to the pool.

    return UpstreamStream(relay(), provider.name, row.modelId, key_id)


# --------------------------------------------------------------------------- #
# Chat completions core
# --------------------------------------------------------------------------- #

def resolve_pipeline(db: Session, requested: str) -> list[ModelRow]:
    """Stage 1 row (direct) + stage 2..N rows, already ordered by the router.

    Kept as a list-returning wrapper so the Anthropic adapter and the legacy
    completions path keep working unchanged; both just need the order.
    """
    rows, _plan = route_request(db, requested)
    return rows


# --------------------------------------------------------------------------- #
# Routing — nova.routing wired to the database
# --------------------------------------------------------------------------- #

def _provider_tags(db: Session) -> dict[str, list[str]]:
    """Per-provider routing tags, stored as `provider_tags` in SystemConfig.

    Kept in config rather than a new column so the schema stays exactly as
    documented in `nova/models.py`; an unset key simply means "no tags".
    """
    from nova.kv import get_config_json

    raw = get_config_json(db, "provider_tags", {}) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): nova_routing.normalize_tags(v) for k, v in raw.items()}


def _usage_counts(db: Session) -> dict[str, int]:
    """Requests served per provider name. Only loaded for the `least-used` strategy."""
    rows = db.execute(
        select(RequestLog.providerName, func.count())
        .where(RequestLog.providerName != "")
        .group_by(RequestLog.providerName)
    ).all()
    return {str(name): int(count) for name, count in rows}


def _candidate(row: ModelRow, stage: int, requested: str,
               tags: dict[str, list[str]], counts: dict[str, int]) -> dict:
    """Flatten a Model row into the plain dict `nova.routing` scores."""
    provider = row.provider
    return {
        "row": row,
        "exposed_id": row.exposedId,
        "model_id": row.modelId,
        "provider_key": provider.key if provider else "",
        "provider_name": provider.name if provider else "",
        "provider_priority": provider.priority if provider else 100,
        "stage": stage,
        "chain_depth": stage,
        "requested": requested,
        "tags": tags.get(provider.key, []) if provider else [],
        "status": row.status or "unknown",
        "latency_ms": int(row.latencyMs or 0),
        "cost_per_1m": max(float(row.priceIn or 0.0), float(row.priceOut or 0.0)) * 1_000_000,
        "req_count": counts.get(provider.name, 0) if provider else 0,
    }


def _effective_route(db: Session, requested: str) -> ModelRoute | None:
    """The enabled route that governs `requested`, else the '*' default chain."""
    route = db.scalars(
        select(ModelRoute).where(ModelRoute.publicId == requested, ModelRoute.enabled.is_(True))
    ).first()
    if route is None:
        route = db.scalars(
            select(ModelRoute).where(ModelRoute.publicId == "*", ModelRoute.enabled.is_(True))
        ).first()
    return route


def auto_standins(db: Session, requested: str, seen: set[str],
                  limit: int = AUTO_STANDIN_LIMIT) -> list[ModelRow]:
    """Healthy, enabled models from other providers — the auto-fallback pool.

    Shared by the live pipeline (after a route's explicit chain is exhausted)
    and by the dashboard's route preview, so what the preview promises is what
    the gateway actually does.

    Sorting: provider priority first, then latency, so the operator's
    preferred providers are tried before later ones — matching the 'priority'
    strategy that `route_request` will re-apply. Free models are NOT pulled
    forward here; the RouterX Free engine is already the pipeline's final
    fallback stage, so crowding auto stand-ins with only free models would let
    a rate-limited free tier consume the entire budget and skip paid providers
    that could still serve the request.
    """
    rows = db.scalars(
        select(ModelRow)
        .where(ModelRow.enabled.is_(True))
        .join(Provider, ModelRow.providerId == Provider.id)
        .options(joinedload(ModelRow.provider))
        .where(Provider.enabled.is_(True))
        .order_by(Provider.priority, ModelRow.latencyMs.asc(), ModelRow.id.asc())
    ).unique().all()
    picked: list[ModelRow] = []
    for m in rows:
        provider = m.provider
        if provider is None or not provider.enabled:
            continue
        # Not reachable through the HTTP upstream path: the free engine is the
        # pipeline's own final stage, gemini is not wired for chat here, and an
        # exchange provider is trading-only with no chat endpoint at all.
        if provider.kind in ("builtin", "gemini", "exchange"):
            continue
        if not provider.baseUrl or provider.baseUrl.startswith("internal://"):
            continue
        if (m.status or "") == "dead":
            continue
        if m.exposedId in seen or m.modelId in seen:
            continue
        seen.add(m.exposedId)
        picked.append(m)
        if len(picked) >= limit:
            break
    return picked


async def _iterate_with_heartbeat(stream, heartbeat: bytes):
    """Iterate a provider stream, yielding heartbeats while it stays silent.

    Yields ``("hb", bytes)`` for every silent interval, ``("piece", item)`` for
    each real chunk and a final ``("eof", None)``. The pending read is shielded
    from cancellation, so emitting a heartbeat never tears the upstream
    connection down the way cancelling ``__anext__`` would. Callers keep the
    same generator across the probe/continuation split, so a long prefill can be
    watched by the client (and a proxy) without the stream dying.
    """
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(stream.__anext__())
            try:
                piece = await asyncio.wait_for(asyncio.shield(pending), timeout=SSE_HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield ("hb", heartbeat)
                continue
            except (StopAsyncIteration, Exception):
                yield ("eof", None)
                return
            pending = None
            yield ("piece", piece)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


def _capped_pairs(db: Session, rows: list[ModelRow]) -> set[tuple[int, str]]:
    """(providerId, modelId) pairs already out of quota - skip them, do not send."""
    keys = {}
    for row in rows:
        provider = row.provider
        if provider is not None:
            keys[provider.id] = provider.key
    if not keys:
        return set()
    return nova_ratelimit.capped(db, provider_keys=keys)


def _profile_chain_ids(db: Session, spec: dict, seen: set[str]) -> list[str]:
    """Model ids a profile's fallback list points at, first match per id."""
    out: list[str] = []
    for ident in spec.get("fallbacks") or []:
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append(ident)
    return out


def resolve_requested(db: Session, requested: str) -> tuple[str, str, dict | None]:
    """`(route_target, profile_name, profile_spec)` for a requested model id.

    `auto` (and any normal model id) routes as itself with no profile. A known
    `auto:<name>` returns its first chain model as the routing target plus the
    profile spec; an unknown profile raises 404 so the client learns about the
    typo instead of silently getting the default chain.
    """
    if nova_profiles.parse_profile_id(requested) in (None, ""):
        return requested, "", None
    name, spec = nova_profiles.resolve_profile(requested, nova_profiles.load_profiles(db))
    if spec is None:
        raise HTTPError(404, f"Unknown routing profile '{name}' - see GET /v1/models for the profiles this gateway exposes")
    chain = spec.get("fallbacks") or []
    target = chain[0] if chain and find_model(db, chain[0]) is not None else "auto"
    return target, name, spec


def _pipeline_rows(db: Session, requested: str,
                   settings: router_settings.RouterSettings,
                   profile: dict | None = None) -> list[ModelRow]:
    """Direct row, then ModelRoute fallbacks, then declarative routing chains.

    `profile` (a named profile's spec) appends its fallback list after the
    operator's chains. A capped (provider, model) pair - out of quota in the
    open usage window - is dropped here, before ordering, so the chain never
    spends a round trip on a request the provider would refuse.
    """
    seen: set[str] = {requested}
    chain_ids: list[str] = []
    for cid in resolve_chain(db, requested):
        if cid and cid not in seen:
            seen.add(cid)
            chain_ids.append(cid)
    for entry in nova_routing.resolve_chain(settings.chains, requested, exclude=seen):
        if entry["id"] not in seen:
            seen.add(entry["id"])
            chain_ids.append(entry["id"])
    # Named-profile fallbacks extend (never replace) the operator's chain.
    profile_spec = profile if isinstance(profile, dict) else None
    if profile_spec:
        for cid in _profile_chain_ids(db, profile_spec, seen):
            chain_ids.append(cid)

    rows: list[ModelRow] = []
    direct = find_model(db, requested)
    # A disabled model must never be dispatched, even when it is the requested
    # id or appears in an explicit route. Auto stand-ins already filter this;
    # the direct/chain paths used to bypass the Models tab toggle.
    if direct is not None and direct.enabled:
        rows.append(direct)
    for cid in chain_ids:
        row = find_model(db, cid)
        if row is not None and row.enabled and row.exposedId not in {r.exposedId for r in rows}:
            rows.append(row)

    # Auto stand-ins — every other healthy, enabled provider joins the chain
    # after the explicit fallbacks (opt out per route with auto=0).
    route = _effective_route(db, requested)
    auto_enabled = True if route is None or route.auto is None else bool(route.auto)
    if profile_spec is not None and "auto_standins" in profile_spec:
        auto_enabled = bool(profile_spec.get("auto_standins")) and auto_enabled
    if auto_enabled:
        rows.extend(auto_standins(db, requested, seen))
    capped = _capped_pairs(db, rows)
    if capped:
        rows = [r for r in rows if (r.provider.id if r.provider else 0, r.modelId) not in capped]
    return rows


def route_request(db: Session, requested: str, body: dict | None = None) -> tuple[list[ModelRow], dict]:
    """Order the candidate pool for one request and explain the decision.

    Returns `(rows, plan)`. `plan` carries the strategy that ran, the policy
    verdict, and one entry per dropped candidate so the dashboard can answer
    "why didn't it use X?" without re-deriving anything.

    Three per-request inputs shape the plan without touching the operator's
    settings:

      * `auto:<profile>` - the profile's fallback list extends the chain and its
        strategy replaces the default for this request,
      * sticky sessions (`x-nova-session`) - the pinned model is tried first but
        never removed, so a failed pin still falls through,
      * quota caps - (provider, model) pairs already out of budget are dropped
        in `_pipeline_rows`.
    """
    settings = router_settings.load(db)
    profile_spec: dict | None = None
    target = requested
    try:
        target, profile_name, profile_spec = resolve_requested(db, requested)
    except HTTPError:
        raise
    except Exception as err:  # noqa: BLE001 - a broken profile must not 500
        log.warning("profile resolution failed: %s", err)
        profile_name, profile_spec = "", None

    rows = _pipeline_rows(db, target, settings, profile=profile_spec)
    tags = _provider_tags(db)
    counts = _usage_counts(db) if settings.strategy == "least-used" else {}

    candidates = [
        _candidate(row, stage, requested, tags, counts)
        for stage, row in enumerate(rows, start=1)
    ]
    request_tags, tag_mode = nova_routing.request_tags(body or {})

    strategy = settings.strategy
    if profile_spec and profile_spec.get("strategy"):
        strategy = str(profile_spec["strategy"])

    pinned = ""
    sid = current_session_id()
    if sid:
        pin = nova_session.get_pin(sid)
        if pin is not None:
            pinned = pin.model

    plan = nova_routing.build_plan(
        candidates,
        requested=requested,
        strategy=strategy,
        weights=settings.weights,
        policies=settings.policies,
        chains=settings.chains,
        denylist=settings.denylist,
        allowlist=settings.allowlist,
        lockouts=nova_routing.LOCKOUTS,
        request_tags_=request_tags,
        tag_mode=tag_mode,
        last_known_good=settings.last_known_good,
        sla=settings.sla,
        pinned=pinned or None,
    )

    if not plan["allowed"]:
        raise HTTPError(403, plan["verdict"].get("reason") or "Blocked by a routing policy")

    plan["rows"] = [c["row"] for c in plan["candidates"]]
    plan["considered"] = len(candidates)
    # Did the requested id resolve to a real candidate? If not, whatever serves
    # the request is a fallback even when it lands at stage 1.
    plan["direct"] = any(
        c.get("exposed_id") == requested or c.get("model_id") == requested
        for c in candidates
    )
    if profile_name:
        plan["profile"] = {
            "name": profile_name,
            "id": f"auto:{profile_name}",
            "strategy": strategy,
            "chain": list(profile_spec.get("fallbacks") or []) if profile_spec else [],
            "note": (profile_spec or {}).get("note", ""),
            "target": target,
        }
        plan["direct"] = True  # the profile id itself is what the client asked for
    if pinned:
        plan["pinned"] = pinned
        _ctx()["pinned"] = pinned
    return plan["rows"], plan


def routing_summary(plan: dict) -> dict:
    """The `_nova.routing` block: what ran, what it skipped, and why."""
    out = {
        "strategy": plan.get("strategy", "priority"),
        "considered": plan.get("considered", 0),
        "ordered": [c.get("exposed_id", "") for c in plan.get("candidates", [])],
        "excluded": plan.get("excluded", {}),
        "steps": plan.get("steps", [])[:8],
        "policies": (plan.get("verdict") or {}).get("applied", []),
    }
    # Named profile (`auto:<name>`) and sticky pin, only when either was in play.
    if isinstance(plan.get("profile"), dict):
        out["profile"] = plan["profile"]
    if plan.get("pinned"):
        out["pinned"] = plan["pinned"]
    return out


def note_attempt(row: ModelRow, ok: bool, reason: str = "") -> None:
    """Feed one upstream attempt back into the lockout registry.

    Repeated failures on the same (provider, model) back the candidate off for
    a while instead of burning a key on every request; a single success clears
    the record so a recovered provider rejoins the pool immediately.
    """
    provider = row.provider
    if not provider:
        return
    if ok:
        nova_routing.LOCKOUTS.record_success(provider.key, row.modelId)
    else:
        nova_routing.LOCKOUTS.record_failure(provider.key, row.modelId, reason or "upstream attempt failed")


def persist_lockouts(db: Session, force: bool = False) -> None:
    """Flush lockout state to SystemConfig, throttled to once a minute."""
    global _LOCKOUT_FLUSH_TS
    now = time.time()
    if not force and now - _LOCKOUT_FLUSH_TS < 60.0:
        return
    _LOCKOUT_FLUSH_TS = now
    router_settings.save_lockouts(db, nova_routing.LOCKOUTS)


async def pipeline_nonstream(db: Session, requested: str, messages: list[dict], temperature: float | None,
                             original_body: dict | None = None, forward_headers: dict[str, str] | None = None,
                             precomputed: tuple[list[ModelRow], dict] | None = None):
    """Returns (result_dict, stage, plan) — walking the router's ordering.

    `precomputed` lets the Anthropic adapter reuse the exact plan it already
    resolved (so a native passthrough decision and the fallback chain cannot
    disagree under a stateful strategy). Any unexpected failure is translated
    into an ``HTTPError`` (502/503) so an agent client receives a well-formed
    gateway error instead of a bare 500.
    """
    started = now_ms()
    if precomputed is not None:
        rows, plan = precomputed
    else:
        try:
            rows, plan = route_request(db, requested, original_body)
        except HTTPError:
            raise
        except SQLAlchemyError as err:
            log.exception("routing failed on a database error")
            raise HTTPError(503, "Gateway database unavailable — retry shortly") from err
        except Exception as err:  # noqa: BLE001
            log.exception("routing failed")
            raise HTTPError(502, f"Gateway routing failed: {err.__class__.__name__}") from err
    stage = 0
    sid = current_session_id()
    pin = nova_session.get_pin(sid) if sid else None
    pinned = pin.model if pin is not None else ""
    rows = nova_session.reorder(rows, pinned)
    for row in rows:
        stage += 1
        result = await attempt_upstream_with_key_retry(
            row, messages, temperature, original_body, forward_headers=forward_headers)
        note_attempt(row, result is not None)
        if result is not None:
            result["stage"] = stage
            _note_handoff(requested, str(result.get("upstream_exposed_id") or row.exposedId),
                          pinned, f"pinned model {pinned} failed")
            return result, stage, plan

    # Final stage: RouterX Free engine (bundled free-model catalogue, non-streaming)
    stage = len(rows) + 1
    tools = None
    tool_choice = None
    if isinstance(original_body, dict):
        tools = original_body.get("tools")
        tool_choice = original_body.get("tool_choice")
    try:
        completion = await nova_engine.chat(
            messages, tools=tools, tool_choice=tool_choice, model=requested
        )
        if isinstance(completion, dict) and completion.get("error"):
            raise nova_engine.EngineUnavailable(str(completion.get("error")))
        engine_meta = completion.get("_nova") if isinstance(completion, dict) else None
        engine_meta = engine_meta if isinstance(engine_meta, dict) else {}
        choice: dict = {}
        if isinstance(completion, dict):
            choices = completion.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        try:
            content = strip_think_blocks(message.get("content"))
        except (KeyError, TypeError, IndexError):
            content = str(completion)
        return {
            "content": content,
            "tool_calls": message.get("tool_calls"),
            "finish_reason": choice.get("finish_reason"),
            "upstream_model": engine_meta.get("upstream_model") or "nova-engine",
            "upstream_exposed_id": engine_meta.get("upstream_model") or "nova-engine",
            "provider_name": engine_meta.get("provider") or "RouterX Free Engine",
            "provider_id": None,
            "via": "nova-engine",
            "usage": {"prompt_tokens": None, "completion_tokens": None},
            "stage": stage,
        }, stage, plan
    except nova_engine.EngineUnavailable as err:
        raise HTTPError(502, f"All routing stages failed: {err}") from err
    except Exception as err:  # noqa: BLE001 — malformed engine payload must not 500
        log.exception("RouterX Free engine returned an unusable response")
        raise HTTPError(502, f"All routing stages failed: engine response error ({err.__class__.__name__})") from err


async def pipeline_stream(db: Session, requested: str, messages: list[dict], temperature: float | None,
                          original_body: dict | None = None, forward_headers: dict[str, str] | None = None,
                          precomputed: tuple[list[ModelRow], dict] | None = None):
    """Yields ``(stage_dict, iterator, stage, plan)`` once — then heartbeats —
    the streaming twin of :func:`pipeline_nonstream`.

    It is an async generator (that is what lets it replay the heartbeats it
    emitted while probing cold stages). Exactly one tuple is yielded: the stage
    that accepted the request, the open piece iterator positioned right after
    the first real delta, the 1-based stage number and the routing plan. Any
    ``("hb", bytes)`` items yielded before it must be relayed verbatim to the
    client so a proxy never sees an idle connection during the warm-up. A total
    failure raises ``HTTPError`` (502/503) instead of yielding a tuple.

    ``stage_dict`` describes the stage that accepted the request and carries the
    same identity keys as a non-streaming result (``upstream_model``,
    ``provider_name``, ``provider_id``, ``upstream_exposed_id``, ``via``,
    ``stage``), so callers can build `_nova` metadata and the RequestLog row
    without re-deriving them. ``iterator`` yields one ``(kind, value)`` pair per
    step from :func:`_iterate_with_heartbeat` — ``("hb", bytes)``, ``("piece",
    dict)`` or ``("eof", None)`` — so the caller keeps the very same generator
    across its probe/continuation split instead of re-wrapping the stream.

    Each routed row is tried in order in streaming mode. A stage is only
    accepted once its first real content arrives; an upstream that opens an SSE
    stream and then goes silent is dropped so the next stage can serve the turn.
    The RouterX Free engine is the final stage. ``precomputed`` reuses a plan the
    adapter already resolved. Unexpected failures surface as ``HTTPError``
    (502/503), never a bare 500.
    """
    if precomputed is not None:
        rows, plan = precomputed
    else:
        try:
            rows, plan = route_request(db, requested, original_body)
        except HTTPError:
            raise
        except SQLAlchemyError as err:
            log.exception("routing failed on a database error")
            raise HTTPError(503, "Gateway database unavailable — retry shortly") from err
        except Exception as err:  # noqa: BLE001
            log.exception("routing failed")
            raise HTTPError(502, f"Gateway routing failed: {err.__class__.__name__}") from err

    stage = 0
    sid = current_session_id()
    pin = nova_session.get_pin(sid) if sid else None
    pinned = pin.model if pin is not None else ""
    rows = nova_session.reorder(rows, pinned)
    for row in rows:
        stage += 1
        opened = None
        failure = None
        block: set[int] = set()
        # Same-model key retry: one extra attempt on the provider's next key.
        for _attempt in range(2):
            try:
                opened = await attempt_upstream_stream(
                    row, messages, temperature, original_body, forward_headers=forward_headers,
                    block=set(block) or None)
            except UpstreamFailure as err:
                failure = err
                await asyncio.to_thread(cooldown_after_failure,
                                        err.provider_id or (row.provider.id if row.provider else None),
                                        err.key_id, err.status, err.retry_after, err.detail)
                if err.key_id is not None:
                    block.add(int(err.key_id))
                log.info("stream stage %s failed: %s", stage, err)
                if err.key_id is not None and err.provider_id is not None and _retry_worthy(err.status):
                    continue
                break
            except Exception as err:  # noqa: BLE001 - one bad stage must not kill the stream
                failure = UpstreamFailure(f"{err.__class__.__name__}: {err}")
                log.warning("stream stage %s crashed: %s: %s", stage, err.__class__.__name__, err)
                break
            break
        if opened is None:
            note_attempt(row, False, failure.detail if failure else "stream could not start")
            continue
        stream, provider_name, upstream_model, key_id = (opened.iterator, opened.provider_name,
                                                         opened.upstream_model, opened.key_id)

        pieces = _iterate_with_heartbeat(stream, b": routerx-keepalive\n\n")
        first_value = None
        try:
            async for kind, value in pieces:
                if kind == "hb":
                    yield value
                    continue
                if kind == "piece" and (value.get("text") or value.get("finish_reason")
                                        or value.get("tool_calls")):
                    first_value = value
                break
        except Exception as err:  # noqa: BLE001 - treat a broken probe as a failed stage
            note_attempt(row, False, f"{err.__class__.__name__}: {err}")
            log.warning("stream stage %s probe crashed: %s: %s", stage, err.__class__.__name__, err)
            await _close_stream(stream)
            continue

        if first_value is None:
            note_attempt(row, False, "stream produced no content before EOF")
            await _close_stream(stream)
            continue

        note_attempt(row, True)
        await asyncio.to_thread(note_key_success, key_id)
        provider = row.provider
        _note_handoff(requested, row.exposedId, pinned, f"pinned model {pinned} failed")
        yield ({
            "upstream_model": upstream_model,
            "upstream_exposed_id": row.exposedId,
            "provider_name": provider_name,
            "provider_id": provider.id if provider is not None else None,
            "via": "upstream-anthropic" if provider is not None and provider.kind == "anthropic" else "upstream",
            "key_id": key_id,
            "stage": stage,
        }, pieces, stage, plan)
        return

    # Final stage: RouterX Free engine (bundled free-model catalogue, streamed).
    stage = len(rows) + 1
    tools = None
    tool_choice = None
    if isinstance(original_body, dict):
        tools = original_body.get("tools")
        tool_choice = original_body.get("tool_choice")
    try:
        iterator = await nova_engine.chat_stream(
            messages, tools=tools, tool_choice=tool_choice, model=requested
        )
    except nova_engine.EngineUnavailable as err:
        raise HTTPError(502, f"All routing stages failed: {err}") from err
    except Exception as err:  # noqa: BLE001 — engine fault is a typed stage failure
        log.exception("RouterX Free engine stream could not start")
        raise HTTPError(502, f"All routing stages failed: engine response error ({err.__class__.__name__})") from err
    yield ({
        "upstream_model": "nova-engine",
        "upstream_exposed_id": "nova-engine",
        "provider_name": "RouterX Free Engine",
        "provider_id": None,
        "via": "nova-engine",
        "stage": stage,
    }, iterator, stage, plan)


async def _close_stream(stream) -> None:
    """Best-effort close of a provider stream the pipeline abandoned."""
    try:
        await stream.aclose()
    except Exception:  # noqa: BLE001 — closing must never fail the request
        pass


# --------------------------------------------------------------------------- #
# GET /v1/models
# --------------------------------------------------------------------------- #

@router.get("/models")
async def list_models(request: Request, discover: str = "", provider: str = "", free: str = "",
                      principal=Depends(nova_auth.gateway_auth)):
    from nova.database import SessionLocal

    with SessionLocal() as db:
        if discover in ("1", "true"):
            try:
                results = await discover_provider_models(db, provider.strip() or None)
            except Exception as err:  # noqa: BLE001 — discovery must not 500 the catalogue
                log.exception("live model discovery failed")
                return JSONResponse({"error": f"Model discovery failed: {err.__class__.__name__}"},
                                    status_code=502)
            summary = aggregate_discovery(results)
            if free in ("1", "true"):
                summary["data"] = [m for m in summary["data"] if m.get("is_free")]
                summary["total_models"] = len(summary["data"])
            return JSONResponse(summary)

        rows = db.execute(
            select(ModelRow)
            .where(ModelRow.enabled.is_(True))
            .options(joinedload(ModelRow.provider))
            .order_by(ModelRow.providerId)
        ).scalars().unique().all()

        data = []
        for m in rows:
            if m.provider is None or not m.provider.enabled:
                continue
            # Exchange providers are trading-only: never advertise a stray
            # Model row as a chat model, whatever put it in the table.
            if m.provider.kind == "exchange":
                continue
            # Exposure allow/deny globs gate advertisement here AND dispatch in
            # nova.routing — one predicate, both chokepoints, so a hidden model
            # can never sneak into a fallback chain either.
            if not nova_routing.is_exposure_allowed(m.exposedId, m.provider.key):
                continue
            try:
                capabilities = json.loads(m.capabilities)
                if not isinstance(capabilities, dict):
                    capabilities = {}
            except Exception:
                capabilities = {}
            data.append({
                "id": m.exposedId,
                "object": "model",
                "owned_by": m.provider.key,
                "context_length": m.contextLength,
                "capabilities": capabilities,
                "is_free": m.isFree,
                "status": m.status,
            })
        # Virtual model ids: `auto` (the default chain) and every named profile
        # (`auto:<name>`). They are clearly marked, owned by "routerx", and sit
        # first so a client listing models sees the routing entry points. They
        # are dispatch targets, never catalogue rows, so identity spoofing (the
        # response `model` equals what was requested) stays intact.
        pseudo = [{
            "id": "auto",
            "object": "model",
            "owned_by": "routerx",
            "context_length": 0,
            "capabilities": {"routing": True, "virtual": True},
            "is_free": False,
            "status": "healthy",
            "virtual": True,
            "description": "Default routing chain - the router picks the provider",
        }]
        stored_profiles = nova_profiles.load_profiles(db)
        for name in sorted(stored_profiles):
            spec = stored_profiles[name]
            pseudo.append({
                "id": f"auto:{name}",
                "object": "model",
                "owned_by": "routerx",
                "context_length": 0,
                "capabilities": {"routing": True, "virtual": True},
                "is_free": False,
                "status": "healthy",
                "virtual": True,
                "description": spec.get("note") or f"Routing profile '{name}'",
                "strategy": spec.get("strategy") or "default",
                "chain": list(spec.get("fallbacks") or []),
            })
        return JSONResponse({
            "object": "list",
            "data": pseudo + data,
            "_nova": {"catalog": nova_catalog.source(), "profiles": nova_profiles.profile_ids(stored_profiles)},
        })


# --------------------------------------------------------------------------- #
# POST /v1/chat/completions
# --------------------------------------------------------------------------- #

@router.post("/chat/completions")
async def chat_completions(request: Request, principal=Depends(nova_auth.gateway_auth)):
    """OpenAI-compatible chat completion (streaming + non-streaming).

    Response cache: opt-in (`NOVA_RESPONSE_CACHE` / the `response_cache` knob)
    and only for non-streaming 200 completions; a hit returns the identical body
    with `_nova.cached = true` and `via = "cache"` so analytics count it.
    Sticky sessions: `x-nova-session` (or `metadata.session_id`) pins the model
    that served the first turn; `x-nova-no-cache: 1` bypasses the cache.
    """
    endpoint = "/v1/chat/completions"
    session_id = nova_session.session_id_from(request.headers, None)
    enter_request(session_id=session_id,
                  client_key_id=getattr(principal, "key_id", None) if principal is not None else None)
    started = now_ms()
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not session_id:
        sid_body = nova_session.session_id_from(request.headers, body)
        if sid_body:
            _ctx()["session_id"] = sid_body

    requested = str(body.get("model") or "").strip()
    raw_messages = body.get("messages")
    if not requested:
        return JSONResponse({"error": "Missing required parameter: 'model'"}, status_code=400)

    refusal = enforce_client(principal, requested)
    if refusal is not None:
        return await auth_refusal(refusal, requested, endpoint)
    if not isinstance(raw_messages, list) or len(raw_messages) == 0:
        return JSONResponse({"error": "Missing required parameter: 'messages' (non-empty array)"}, status_code=400)

    messages = normalize_messages(raw_messages)
    temperature = body.get("temperature") if isinstance(body.get("temperature"), (int, float)) else None
    stream = body.get("stream") is True
    tokens_in = estimate_tokens("\n".join(f"{m['role']}:{m['content']}" for m in messages))
    # Forward Anthropic-format headers (e.g. anthropic-beta for safeguards billing) to upstream.
    forward_headers = extract_forward_headers(request=request)

    from nova.database import SessionLocal

    no_cache = str(request.headers.get("x-nova-no-cache") or "").strip() in ("1", "true", "yes")
    cache_enabled = (not stream) and (not no_cache) and nova_cache.is_enabled()
    cache_key = nova_cache.key_for(body, messages) if cache_enabled else ""

    if cache_enabled:
        hit = nova_cache.get(cache_key)
        if hit is not None:
            hit["id"] = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            hit["created"] = int(time.time())
            meta = hit.get("_nova") if isinstance(hit.get("_nova"), dict) else {}
            meta["cached"] = True
            hit["_nova"] = meta
            usage = hit.get("usage") if isinstance(hit.get("usage"), dict) else {}
            with SessionLocal() as db:
                write_log(db, model=requested, provider_name=str(meta.get("provider") or "cache"),
                          status=200, latency_ms=now_ms() - started,
                          tokens_in=int(usage.get("prompt_tokens") or 0),
                          tokens_out=int(usage.get("completion_tokens") or 0),
                          via="cache", spoofed=bool(meta.get("spoofed")))
                attribute_client(db, principal, 0, 0)
            return JSONResponse(hit)

    if stream:
        return StreamingResponse(
            _chat_stream_response(requested, messages, temperature, tokens_in, started, body, forward_headers,
                                  client_key_id=getattr(principal, "key_id", None) if principal is not None else None),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **build_headers()},
        )

    with SessionLocal() as db:
        try:
            result, stage, plan = await pipeline_nonstream(db, requested, messages, temperature, body, forward_headers)
        except HTTPError as err:
            write_log(db, model=requested, upstream_model="nova-engine",
                      provider_name="RouterX Free Engine", endpoint=endpoint,
                      status=err.status, latency_ms=now_ms() - started, via="nova-engine",
                      error=err.message)
            return JSONResponse({"error": err.message}, status_code=err.status)
        except Exception as err:  # noqa: BLE001 - last line of defence, never a bare 500
            log.exception("unhandled error in /v1/chat/completions")
            write_log(db, model=requested, upstream_model="nova-engine",
                      provider_name="RouterX Free Engine", endpoint=endpoint,
                      status=502, latency_ms=now_ms() - started, via="nova-engine",
                      error=f"{err.__class__.__name__}: {err}")
            return JSONResponse({"error": f"Gateway failed to serve the request ({err.__class__.__name__})"},
                                status_code=502)
        persist_lockouts(db)

        completion_text = strip_think_blocks(result.get("content") or "") or ""
        prompt_tokens = result["usage"]["prompt_tokens"] or tokens_in
        completion_tokens = result["usage"]["completion_tokens"] or estimate_tokens(completion_text)
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        latency = now_ms() - started
        meta = nova_meta(result["upstream_model"], result["provider_name"], result["stage"], requested,
                         result["stage"] > 1 or not plan.get("direct", True), routing_summary(plan),
                         handoff=current_handoff())
        meta["latency_ms"] = latency
        first_token_ms = int(result.get("ttft_ms") or 0)
        if first_token_ms:
            meta["ttft_ms"] = first_token_ms
        write_log(db, model=requested, upstream_model=result["upstream_model"],
                  provider_name=result["provider_name"], status=200,
                  latency_ms=latency, tokens_in=prompt_tokens,
                  tokens_out=completion_tokens, via=result["via"], spoofed=meta["spoofed"],
                  ttft_ms=first_token_ms)
        record_served(db, requested, result, messages, prompt_tokens, completion_tokens)
        attribute_client(db, principal, prompt_tokens, completion_tokens)

        msg: dict = {"role": "assistant", "content": strip_think_blocks(result.get("content"))}
        if result.get("tool_calls"):
            msg["tool_calls"] = result["tool_calls"]
        finish_reason = result.get("finish_reason") or ("tool_calls" if result.get("tool_calls") else "stop")
        response_body = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": finish_reason}],
            "usage": usage,
            "_nova": meta,
        }
        if cache_enabled and nova_cache.cacheable(response_body):
            nova_cache.put(cache_key, response_body)
        return JSONResponse(response_body)


def sse_chunk(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


async def _chat_stream_response(requested: str, messages: list[dict], temperature, tokens_in: int, started: int,
                                original_body: dict | None = None, forward_headers: dict[str, str] | None = None,
                                client_key_id: int | None = None, session_id: str = ""):
    """Full pipeline with SSE output. Commits to a stage only after the first
    real content arrives; otherwise falls through to the next stage.

    Records TTFT (time to the first real content delta) in the RequestLog row,
    books usage per (provider, model, key) and per client key, and - when a
    sticky session is in play - reports a model switch through `_nova.handoff`
    in the stream's final metadata (the SSE chunk stream itself is unchanged).
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    sid = session_id or current_session_id()
    ctx = enter_request(session_id=sid, client_key_id=client_key_id)
    ttft_ms = 0

    def base_chunk() -> dict:
        return {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": requested, "choices": []}

    from nova.database import SessionLocal

    db = SessionLocal()
    committed = False
    assembled: list[str] = []
    served = {"upstream_model": "nova-engine", "provider_name": "RouterX Free Engine", "via": "nova-engine", "stage": 0}
    think_filter = ThinkStreamFilter()
    try:
        # Open the client stream immediately so a proxy never sees an idle
        # connection while the router resolves and the upstream warms up.
        yield b": routerx-keepalive\n\n"
        try:
            rows, plan = route_request(db, requested, original_body)
        except HTTPError as err:
            yield sse_chunk({"error": {"message": err.message, "type": "gateway_error",
                                       "code": err.status}})
            yield b"data: [DONE]\n\n"
            return
        except Exception as err:  # noqa: BLE001
            log.exception("stream routing failed")
            yield sse_chunk({"error": {"message": f"Gateway routing failed: {err.__class__.__name__}",
                                       "type": "gateway_error", "code": 502}})
            yield b"data: [DONE]\n\n"
            return
        stage = 0
        for row in rows:
            stage += 1
            try:
                opened = await attempt_upstream_stream(
                    row, messages, temperature, original_body, forward_headers=forward_headers)
                # `UpstreamStream` still iterates as the historical 3-tuple for
                # older call sites, so read the identity off its attributes --
                # unpacking four values here raises ValueError ("expected 4,
                # got 3"), which used to lock the provider out and fall through
                # to the free engine on every streaming request.
                stream, provider_name, upstream_model, key_id = (
                    opened.iterator, opened.provider_name, opened.upstream_model, opened.key_id)
            except UpstreamFailure as err:
                note_attempt(row, False, err.detail)
                await asyncio.to_thread(cooldown_after_failure,
                                        err.provider_id or (row.provider.id if row.provider else None),
                                        err.key_id, err.status, err.retry_after, err.detail)
                log.info("stream stage %s failed: %s", stage, err)
                continue
            except Exception as err:  # noqa: BLE001
                note_attempt(row, False, f"{err.__class__.__name__}: {err}")
                log.warning("stream stage %s crashed: %s: %s", stage, err.__class__.__name__, err)
                continue

            # Probe the first delta before committing — heartbeats keep the
            # connection warm while a cold/long-thinking upstream stays silent.
            pieces = _iterate_with_heartbeat(stream, b": routerx-keepalive\n\n")
            first = None
            async for kind, value in pieces:
                if kind == "hb":
                    yield value
                    continue
                if kind == "piece" and (value["text"] or value["finish_reason"] or value.get("tool_calls")):
                    first = value
                break

            if first is None:
                try:
                    await stream.aclose()
                except Exception:
                    pass
                continue

            committed = True
            note_attempt(row, True)
            served = {"upstream_model": upstream_model, "provider_name": provider_name,
                      "via": "upstream", "stage": stage}
            served["provider_id"] = row.provider.id if row.provider is not None else None
            served["upstream_exposed_id"] = row.exposedId
            served["key_id"] = key_id
            ttft_ms = now_ms() - started
            _note_handoff(requested, row.exposedId, str(ctx.get("pinned") or ""),
                          f"pinned model {ctx.get('pinned') or ''} failed")
            # Track tool_calls for streaming passthrough
            tool_calls_buffer: list[dict] = []
            finish_reason_val = None
            yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
            # handle first piece
            if first["text"]:
                filtered = think_filter.feed(first["text"])
                if filtered:
                    assembled.append(filtered)
                    yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"content": filtered}, "finish_reason": None}]})
            if first.get("tool_calls"):
                tool_calls_buffer.extend(first["tool_calls"])
                yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"tool_calls": first["tool_calls"]}, "finish_reason": None}]})
            if first.get("finish_reason"):
                finish_reason_val = first["finish_reason"]
                yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason_val}]})
                yield b"data: [DONE]\n\n"
                try:
                    await stream.aclose()
                except Exception:
                    pass
                break
            async for kind, piece in pieces:
                if kind == "hb":
                    yield piece
                    continue
                if kind == "eof":
                    break
                if piece["text"]:
                    filtered = think_filter.feed(piece["text"])
                    if filtered:
                        assembled.append(filtered)
                        yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"content": filtered}, "finish_reason": None}]})
                if piece.get("tool_calls"):
                    tool_calls_buffer.extend(piece["tool_calls"])
                    yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"tool_calls": piece["tool_calls"]}, "finish_reason": None}]})
                if piece["finish_reason"]:
                    finish_reason_val = piece["finish_reason"]
                    break
            final_reason = finish_reason_val or ("tool_calls" if tool_calls_buffer else "stop")
            yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {}, "finish_reason": final_reason}]})
            yield b"data: [DONE]\n\n"
            break

        if not committed:
            # Final stage: RouterX Free engine (bundled free-model catalogue, streamed).
            stage = len(rows) + 1
            served = {"upstream_model": "nova-engine", "provider_name": "RouterX Free Engine",
                      "via": "nova-engine", "stage": stage}
            tools = None
            tool_choice = None
            if isinstance(original_body, dict):
                tools = original_body.get("tools")
                tool_choice = original_body.get("tool_choice")
            try:
                iterator = await nova_engine.chat_stream(
                    messages, tools=tools, tool_choice=tool_choice, model=requested
                )
                yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
                finish_reason_val = None
                engine_tool_calls: list[dict] = []
                async for data in iterator:
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    engine_meta = chunk.get("_nova")
                    if isinstance(engine_meta, dict):
                        served["upstream_model"] = engine_meta.get("upstream_model") or served["upstream_model"]
                        served["provider_name"] = engine_meta.get("provider") or served["provider_name"]
                    if chunk.get("error"):
                        raise nova_engine.EngineUnavailable(str(chunk.get("error", "engine error")))
                    choices = chunk.get("choices")
                    text = ""
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
                        text = delta.get("content") or ""
                        tool_calls_delta = delta.get("tool_calls")
                        if isinstance(tool_calls_delta, list) and tool_calls_delta:
                            engine_tool_calls.extend(tool_calls_delta)
                            yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"tool_calls": tool_calls_delta}, "finish_reason": None}]})
                        if choices[0].get("finish_reason"):
                            finish_reason_val = choices[0]["finish_reason"]
                        if not text:
                            msg = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
                            text = msg.get("content") or ""
                    if isinstance(text, str) and text:
                        filtered = think_filter.feed(text)
                        if filtered:
                            assembled.append(filtered)
                            yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {"content": filtered}, "finish_reason": None}]})
                committed = True
                if not ttft_ms:
                    ttft_ms = now_ms() - started
                final_reason = finish_reason_val or ("tool_calls" if engine_tool_calls else "stop")
                yield sse_chunk({**base_chunk(), "choices": [{"index": 0, "delta": {}, "finish_reason": final_reason}]})
                yield b"data: [DONE]\n\n"
            except nova_engine.EngineUnavailable as err:
                msg = f"All routing stages failed: {err}. Fix checklist: (1) Dashboard → Providers → Test your provider; (2) Dashboard → Models → Sync models; (3) request a model id that exists there (e.g. \"routerx/air\")."
                yield sse_chunk({"error": {"message": msg, "type": "gateway_error", "code": 502}})
                yield b"data: [DONE]\n\n"
            except Exception as err:  # noqa: BLE001
                log.exception("RouterX Free engine stream failed")
                yield sse_chunk({"error": {"message": f"All routing stages failed: engine response error ({err.__class__.__name__})",
                                           "type": "gateway_error", "code": 502}})
                yield b"data: [DONE]\n\n"
    except Exception as err:  # noqa: BLE001 — headers are already sent; emit a clean SSE error
        log.exception("unhandled error in /v1/chat/completions stream")
        try:
            yield sse_chunk({"error": {"message": f"Gateway stream failed: {err.__class__.__name__}",
                                       "type": "gateway_error", "code": 502}})
            yield b"data: [DONE]\n\n"
        except Exception:
            pass
    finally:
        content = "".join(assembled)
        tokens_out = estimate_tokens(content) or 1
        persist_lockouts(db)
        if committed or assembled:
            write_log(db, model=requested, upstream_model=served["upstream_model"],
                      provider_name=served["provider_name"], status=200,
                      latency_ms=now_ms() - started,
                      tokens_in=tokens_in, tokens_out=tokens_out,
                      via=served["via"], spoofed=requested != served["upstream_model"],
                      ttft_ms=ttft_ms)
            try:
                record_served(db, requested, {
                    **served, "upstream_model": served["upstream_model"],
                }, messages, tokens_in, tokens_out)
            except Exception:
                pass
            try:
                nova_auth.update_usage(db, client_key_id, tokens_in, tokens_out)
            except Exception:
                pass
        db.close()


# --------------------------------------------------------------------------- #
# POST /v1/completions — legacy
# --------------------------------------------------------------------------- #

@router.post("/completions")
async def legacy_completions(request: Request, principal=Depends(nova_auth.gateway_auth)):
    started = now_ms()
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    requested = str(body.get("model") or "").strip()
    prompt = body.get("prompt")
    if not requested:
        return JSONResponse({"error": "Missing required parameter: 'model'"}, status_code=400)
    refusal = enforce_client(principal, requested)
    if refusal is not None:
        return await auth_refusal(refusal, requested, "/v1/completions")
    if prompt is None:
        return JSONResponse({"error": "Missing required parameter: 'prompt'"}, status_code=400)

    prompts = prompt if isinstance(prompt, list) else [prompt]
    if not prompts or not all(isinstance(p, str) for p in prompts):
        return JSONResponse({"error": "'prompt' must be a string or array of strings"}, status_code=400)
    if body.get("stream") is True and len(prompts) > 1:
        return JSONResponse({"error": "Streaming is supported for a single prompt only"}, status_code=400)

    temperature = body.get("temperature") if isinstance(body.get("temperature"), (int, float)) else None

    if body.get("stream") is True:
        return StreamingResponse(
            _legacy_stream(requested, prompts[0], temperature, started),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **build_headers()},
        )

    from nova.database import SessionLocal

    choices = []
    total_in = total_out = 0
    with SessionLocal() as db:
        for i, p in enumerate(prompts):
            messages = normalize_messages([{"role": "user", "content": p}])
            try:
                result, stage, plan = await pipeline_nonstream(db, requested, messages, temperature)
            except HTTPError as err:
                write_log(db, model=requested, upstream_model="nova-engine",
                          provider_name="RouterX Free Engine", endpoint="/v1/completions",
                          status=err.status, latency_ms=now_ms() - started, via="nova-engine",
                          error=err.message)
                return JSONResponse({"error": err.message}, status_code=err.status)
            tin = result["usage"]["prompt_tokens"] or estimate_tokens(p)
            tout = result["usage"]["completion_tokens"] or estimate_tokens(result["content"])
            total_in += tin
            total_out += tout
            choices.append({"text": strip_think_blocks(result["content"]) or "", "index": i, "finish_reason": "stop", "logprobs": None})
            if i == 0:
                served = result
        write_log(db, model=requested, upstream_model=served["upstream_model"],
                  provider_name=served["provider_name"], endpoint="/v1/completions",
                  status=200, latency_ms=now_ms() - started, tokens_in=total_in,
                  tokens_out=total_out, via=served["via"],
                  spoofed=requested != served["upstream_model"])
        persist_lockouts(db)
        record_served(db, requested, served, None, total_in, total_out)
        attribute_client(db, principal, total_in, total_out)

    return JSONResponse({
        "id": f"cmpl-{uuid.uuid4().hex[:24]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": requested,
        "choices": choices,
        "usage": {"prompt_tokens": total_in, "completion_tokens": total_out,
                  "total_tokens": total_in + total_out},
        "_nova": nova_meta(served["upstream_model"], served["provider_name"], served["stage"], requested,
                           served["stage"] > 1 or not plan.get("direct", True), routing_summary(plan)),
    })


async def _legacy_stream(requested: str, prompt: str, temperature, started: int):
    completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    messages = normalize_messages([{"role": "user", "content": prompt}])
    assembled: list[str] = []
    from nova.database import SessionLocal
    db = SessionLocal()
    committed = False
    served = {"upstream_model": "nova-engine", "provider_name": "RouterX Free Engine", "via": "nova-engine", "stage": 0}
    legacy_think = ThinkStreamFilter()
    try:
        try:
            rows = resolve_pipeline(db, requested)
        except Exception as err:  # noqa: BLE001 — keep the SSE contract on routing failures
            log.exception("legacy stream routing failed")
            yield sse_chunk({"error": {"message": f"Gateway routing failed: {err.__class__.__name__}",
                                       "type": "gateway_error", "code": 502}})
            yield b"data: [DONE]\n\n"
            return
        stage = 0
        for row in rows:
            stage += 1
            try:
                opened = await attempt_upstream_stream(row, messages, temperature)
                stream, provider_name, upstream_model = opened
            except UpstreamFailure:
                continue
            except Exception as err:  # noqa: BLE001
                log.warning("legacy stream stage %s crashed: %s", stage, err)
                continue
            pieces = _iterate_with_heartbeat(stream, b": routerx-keepalive\n\n")
            first = None
            async for kind, value in pieces:
                if kind == "hb":
                    yield value
                    continue
                if kind == "piece" and (value["text"] or value["finish_reason"]):
                    first = value
                break
            if first is None:
                try:
                    await stream.aclose()
                except Exception:
                    pass
                continue
            committed = True
            served = {"upstream_model": upstream_model, "provider_name": provider_name, "via": "upstream", "stage": stage}

            def text_chunk(t: str, finish=None) -> bytes:
                return sse_chunk({"id": completion_id, "object": "text_completion.chunk",
                                  "created": created, "model": requested,
                                  "choices": [{"text": t, "index": 0, "finish_reason": finish, "logprobs": None}]})

            if first["text"]:
                filtered = legacy_think.feed(first["text"])
                if filtered:
                    assembled.append(filtered)
                    yield text_chunk(filtered)
            if first.get("finish_reason"):
                yield text_chunk("", "stop")
                yield b"data: [DONE]\n\n"
                try:
                    await stream.aclose()
                except Exception:
                    pass
                break
            async for kind, piece in pieces:
                if kind == "hb":
                    yield piece
                    continue
                if kind == "eof":
                    break
                if piece["text"]:
                    filtered = legacy_think.feed(piece["text"])
                    if filtered:
                        assembled.append(filtered)
                        yield text_chunk(filtered)
                if piece["finish_reason"]:
                    break
            yield text_chunk("", "stop")
            yield b"data: [DONE]\n\n"
            break

        if not committed:
            stage = len(rows) + 1
            served = {"upstream_model": "nova-engine", "provider_name": "RouterX Free Engine", "via": "nova-engine", "stage": stage}
            try:
                result_content = ""
                completion = await nova_engine.chat(messages, model=requested)
                try:
                    result_content = strip_think_blocks(completion["choices"][0]["message"]["content"])
                except (KeyError, TypeError, IndexError):
                    result_content = strip_think_blocks(str(completion))
                committed = True
                # result_content already strip_think_blocks'd but also run through filter for safety
                __f = legacy_think.feed(result_content)
                if __f:
                    assembled.append(__f)
                    result_content = __f
                else:
                    result_content = ''

                def text_chunk(t: str, finish=None) -> bytes:
                    return sse_chunk({"id": completion_id, "object": "text_completion.chunk",
                                      "created": created, "model": requested,
                                      "choices": [{"text": t, "index": 0, "finish_reason": finish, "logprobs": None}]})

                # chunk the completed text for a streaming experience
                step = 24
                for i in range(0, len(result_content), step):
                    yield text_chunk(result_content[i:i + step])
                yield text_chunk("", "stop")
                yield b"data: [DONE]\n\n"
            except nova_engine.EngineUnavailable as err:
                yield sse_chunk({"error": {"message": f"All routing stages failed: {err}", "type": "gateway_error", "code": 502}})
                yield b"data: [DONE]\n\n"
            except Exception as err:  # noqa: BLE001
                log.exception("RouterX Free engine legacy stream failed")
                yield sse_chunk({"error": {"message": f"All routing stages failed: engine response error ({err.__class__.__name__})",
                                           "type": "gateway_error", "code": 502}})
                yield b"data: [DONE]\n\n"
    except Exception as err:  # noqa: BLE001 — headers already sent
        log.exception("unhandled error in /v1/completions stream")
        try:
            yield sse_chunk({"error": {"message": f"Gateway stream failed: {err.__class__.__name__}",
                                       "type": "gateway_error", "code": 502}})
            yield b"data: [DONE]\n\n"
        except Exception:
            pass
    finally:
        if assembled:
            write_log(db, model=requested, upstream_model=served["upstream_model"],
                      provider_name=served["provider_name"], endpoint="/v1/completions",
                      status=200, latency_ms=now_ms() - started,
                      tokens_in=estimate_tokens(prompt), tokens_out=estimate_tokens("".join(assembled)),
                      via=served["via"], spoofed=requested != served["upstream_model"])
        db.close()


# --------------------------------------------------------------------------- #
# POST /v1/embeddings
# --------------------------------------------------------------------------- #

@router.post("/embeddings")
async def embeddings(request: Request, principal=Depends(nova_auth.gateway_auth)):
    started = now_ms()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    requested = str(body.get("model") or "").strip()
    raw_input = body.get("input")
    if not requested:
        return JSONResponse({"error": "Missing required parameter: 'model'"}, status_code=400)
    refusal = enforce_client(principal, requested)
    if refusal is not None:
        return await auth_refusal(refusal, requested, "/v1/embeddings")
    if raw_input is None:
        return JSONResponse({"error": "Missing required parameter: 'input'"}, status_code=400)

    inputs = raw_input if isinstance(raw_input, list) else [raw_input]
    inputs = [i if isinstance(i, str) else json.dumps(i) for i in inputs]

    from nova.database import SessionLocal

    with SessionLocal() as db:
        row = find_model(db, requested)
        if row is None or not row.enabled or row.provider is None or not row.provider.enabled:
            return JSONResponse({
                "error": f"Model '{requested}' is not configured or is disabled. Sync models in the dashboard or pick one from GET /v1/models.",
            }, status_code=404)
        provider = row.provider
        key_info = await asyncio.to_thread(select_upstream_key_sync, provider.id)
        base = (provider.baseUrl or "").rstrip("/")

        if provider.kind == "openai" and key_info and base and not base.startswith("internal://"):
            try:
                res = await NONSTREAM_CLIENT.post(
                    f"{base}/embeddings",
                    headers={"Authorization": f"Bearer {key_info['apiKey']}", "Content-Type": "application/json"},
                    json={"model": row.modelId, "input": inputs},
                )
            except httpx.HTTPError as err:
                write_log(db, model=requested, provider_name=provider.name, endpoint="/v1/embeddings",
                          status=502, latency_ms=now_ms() - started, error=str(err))
                return JSONResponse({"error": f"Upstream embeddings call failed: {err}"}, status_code=502)
            if res.status_code < 200 or res.status_code >= 300:
                write_log(db, model=requested, provider_name=provider.name, endpoint="/v1/embeddings",
                          status=res.status_code, latency_ms=now_ms() - started,
                          error=f"upstream HTTP {res.status_code}")
                return JSONResponse({"error": f"Upstream returned HTTP {res.status_code} for embeddings"},
                                    status_code=502)
            data = response_json_dict(res)
            if data is None:
                return JSONResponse({"error": "Upstream returned a non-JSON 2xx body for embeddings"},
                                    status_code=502)
            write_log(db, model=requested, upstream_model=row.modelId, provider_name=provider.name,
                      endpoint="/v1/embeddings", status=200, latency_ms=now_ms() - started,
                      tokens_in=estimate_tokens(" ".join(inputs)), tokens_out=0, via="upstream",
                      spoofed=requested != row.modelId)
            nova_ratelimit.record_usage(db, provider.id, row.modelId, key_info.get("keyId"),
                                        estimate_tokens(" ".join(inputs)), 0)
            attribute_client(db, principal, estimate_tokens(" ".join(inputs)), 0)
            return JSONResponse(data)

        if provider.kind == "gemini" and key_info and base and not base.startswith("internal://"):
            vectors: list[dict] = []
            try:
                async with httpx.AsyncClient(timeout=NONSTREAM_READ_TIMEOUT) as client:
                    for i, text in enumerate(inputs):
                        res = await client.post(
                            f"{base}/models/{row.modelId}:embedContent?key={key_info['apiKey']}",
                            json={"content": {"parts": [{"text": text}]}},
                        )
                        if res.status_code < 200 or res.status_code >= 300:
                            return JSONResponse(
                                {"error": f"Upstream returned HTTP {res.status_code} for embeddings"},
                                status_code=502)
                        payload = response_json_dict(res) or {}
                        embedding = payload.get("embedding") if isinstance(payload.get("embedding"), dict) else {}
                        values = embedding.get("values", [])
                        vectors.append({"object": "embedding", "index": i, "embedding": values})
            except httpx.HTTPError:
                # Never echo the exception: the Gemini URL carries the API key.
                return JSONResponse({"error": "Upstream embeddings call failed (connection error)"},
                                    status_code=502)
            write_log(db, model=requested, upstream_model=row.modelId, provider_name=provider.name,
                      endpoint="/v1/embeddings", status=200, latency_ms=now_ms() - started,
                      tokens_in=estimate_tokens(" ".join(inputs)), tokens_out=0, via="upstream",
                      spoofed=requested != row.modelId)
            return JSONResponse({
                "object": "list", "data": vectors, "model": requested,
                "usage": {"prompt_tokens": estimate_tokens(" ".join(inputs)), "total_tokens": estimate_tokens(" ".join(inputs))},
            })

        return JSONResponse({
            "error": ("Embeddings require an OpenAI-compatible or Gemini provider with a working key. "
                      f"'{requested}' resolved to provider '{provider.key}' (kind={provider.kind}) which cannot serve embeddings."),
        }, status_code=502)


# --------------------------------------------------------------------------- #
# Anthropic-format adapter — POST /v1/messages (+ /messages/count_tokens)
#
# Kept in its own module (routers/anthropic_adapter.py) and included here so it
# is mounted under the same /v1 + /api/v1 prefixes as the rest of the gateway.
# It is imported at the END of this module because it resolves the pipeline
# helpers above lazily on first use.
# --------------------------------------------------------------------------- #

from .anthropic_adapter import router as anthropic_router  # noqa: E402
from .openai_responses import router as responses_router  # noqa: E402
from .trading import router as trading_router  # noqa: E402

router.include_router(anthropic_router)
router.include_router(responses_router)
router.include_router(trading_router)
