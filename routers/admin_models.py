"""Admin model catalogue — list, toggle, health ping and live sync.

Python port of:
  src/app/api/admin/models/route.ts           (GET with filters)
  src/app/api/admin/models/[id]/route.ts      (PATCH toggle)
  src/app/api/admin/models/[id]/ping/route.ts (POST health check — real probe)
  src/app/api/admin/models/sync/route.ts      (POST live catalogue sync)

Mounted at /api/admin/models by main.py.
"""
from __future__ import annotations

import random
import time

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from nova import engine as nova_engine
from nova.database import SessionLocal, get_db
from nova.models import Model as ModelRow, ProviderKey, utcnow
from nova.syncengine import sync_providers

from ._common import epoch_ms, invalid_id, not_found, parse_caps, parse_int

router = APIRouter()

PING_TIMEOUT_S = 15.0

# A ping only proves the endpoint is reachable. The probe below sends a REAL
# one-shot question so "healthy" means the model actually responds.
PROBE_QUESTIONS = [
    "Reply with exactly one word: OK",
    "What is 2+2? Reply with just the number.",
    "Say hello.",
    "Reply with a single word: pong",
    "Name any color. One word only.",
    "Reply with the word: alive",
]

MODEL_STATUSES = ("healthy", "cooling", "dead", "unknown")
CAPABILITIES = ("tools", "vision", "reasoning")


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #

def status_detail(status: str, http_status: int) -> str:
    """Human-readable check detail synthesized from status + last HTTP code."""
    if status == "healthy":
        return f"HTTP {http_status} — upstream OK" if http_status > 0 else "Healthy"
    if status == "cooling":
        return f"HTTP {http_status} — cooling down" if http_status > 0 else "Cooling down"
    if status == "dead":
        return f"HTTP {http_status} — upstream rejected" if http_status > 0 else "Unreachable / offline"
    return f"Last check: HTTP {http_status}" if http_status > 0 else "Not checked yet"


def to_model(m: ModelRow, provider_name: str, provider_color: str) -> dict:
    return {
        "id": m.id,
        "provider_id": m.providerId,
        "provider_name": provider_name,
        "provider_color": provider_color,
        "model_id": m.modelId,
        "exposed_id": m.exposedId,
        "display_name": m.displayName,
        "is_free": m.isFree,
        "status": m.status,
        "http_status": m.httpStatus,
        "detail": status_detail(m.status, m.httpStatus),
        "latency_ms": m.latencyMs,
        "checked_at": epoch_ms(m.checkedAt),
        "enabled": m.enabled,
        "context_length": m.contextLength,
        "max_output": m.maxOutput,
        "capabilities": parse_caps(m.capabilities),
        "price_in": m.priceIn,
        "price_out": m.priceOut,
        "description": m.description,
    }


# --------------------------------------------------------------------------- #
# GET /api/admin/models?provider_id&status&search&capability&free[&limit&offset]
# Ordered by provider priority asc, then exposed_id.
# --------------------------------------------------------------------------- #

@router.get("")
def list_models(req: Request, db: Session = Depends(get_db)):
    sp = req.query_params

    provider_id: int | None = None
    if sp.get("provider_id") is not None:
        provider_id = parse_int(sp.get("provider_id"))

    status_raw = sp.get("status")
    status = status_raw if status_raw in MODEL_STATUSES else None

    free_raw = sp.get("free")
    free: bool | None = None
    if free_raw in ("true", "1"):
        free = True
    elif free_raw in ("false", "0"):
        free = False

    show_disabled_raw = sp.get("show_disabled")
    show_disabled = show_disabled_raw in ("true", "1")

    search = (sp.get("search") or "").strip().lower()
    capability = sp.get("capability")
    capability_filter = capability if capability in CAPABILITIES else None

    # Optional pagination (extension — the dashboard fetches without it).
    limit: int | None = parse_int(sp.get("limit"))
    offset = parse_int(sp.get("offset")) or 0
    if limit is not None and limit < 0:
        limit = None
    if offset < 0:
        offset = 0

    stmt = select(ModelRow).options(joinedload(ModelRow.provider))
    if provider_id is not None:
        stmt = stmt.where(ModelRow.providerId == provider_id)
    if status is not None:
        stmt = stmt.where(ModelRow.status == status)
    if free is not None:
        stmt = stmt.where(ModelRow.isFree == free)
    if not show_disabled:
        stmt = stmt.where(ModelRow.enabled.is_(True))
    rows = db.scalars(stmt).unique().all()

    filtered = []
    for m in rows:
        if search:
            hay = f"{m.exposedId}\n{m.displayName}\n{m.modelId}".lower()
            if search not in hay:
                continue
        if capability_filter:
            caps = parse_caps(m.capabilities)
            if caps.get(capability_filter) is not True:
                continue
        filtered.append(m)

    filtered.sort(key=lambda m: (m.provider.priority, m.exposedId))

    window = filtered[offset:] if offset > 0 else filtered
    if limit is not None:
        window = window[:limit]

    return JSONResponse({
        "total": len(filtered),
        "rows": [to_model(m, m.provider.name, m.provider.color) for m in window],
    })


# --------------------------------------------------------------------------- #
# POST /api/admin/models/sync — REAL live catalogue sync (must precede /{id})
# --------------------------------------------------------------------------- #

@router.post("/sync")
async def sync_models(req: Request):
    sp = req.query_params
    provider_id: int | None = None
    if sp.get("provider_id") is not None:
        provider_id = parse_int(sp.get("provider_id"))

    with SessionLocal() as db:
        reports = await sync_providers(db, provider_id)

    synced = sum(r["discovered"] for r in reports)
    new_added = sum(r["created"] for r in reports)
    updated = sum(r["updated"] for r in reports)
    failed = [r for r in reports if not r["ok"]]

    payload: dict = {
        "ok": len(failed) == 0 or new_added + updated > 0,
        "synced": synced,
        "new_added": new_added,
        "updated": updated,
        "providers": reports,
    }
    if failed:
        payload["errors"] = [f"{r['provider']}: {r.get('error') or 'failed'}" for r in failed]

    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# POST /api/admin/models/disable-unreachable — bulk-disable every model whose
# last health check marked it dead. Disabled models are excluded from the
# gateway catalogue and the RouterX Agent, so unreachable models never get
# picked for real traffic.
# --------------------------------------------------------------------------- #

@router.post("/disable-unreachable")
def disable_unreachable(db: Session = Depends(get_db)):
    dead = db.scalars(
        select(ModelRow).where(ModelRow.status == "dead", ModelRow.enabled.is_(True))
    ).all()
    for m in dead:
        m.enabled = False
    db.commit()
    return JSONResponse({
        "ok": True,
        "disabled": len(dead),
        "ids": [m.id for m in dead],
        "exposed_ids": [m.exposedId for m in dead],
    })


# --------------------------------------------------------------------------- #
# GET /api/admin/models/{id} — one model for the detail dialog. Avoid sorting
# and serializing the entire catalogue each time a card is opened.
# --------------------------------------------------------------------------- #

@router.get("/{id}")
def get_model(id: str, db: Session = Depends(get_db)):
    model_id = parse_int(id)
    if model_id is None:
        return invalid_id("Invalid model id")
    model = db.scalars(
        select(ModelRow).where(ModelRow.id == model_id).options(joinedload(ModelRow.provider))
    ).first()
    if model is None:
        return not_found("Model not found")
    return JSONResponse(to_model(model, model.provider.name, model.provider.color))


# --------------------------------------------------------------------------- #
# PATCH /api/admin/models/{id} — toggle a model on/off
# --------------------------------------------------------------------------- #

@router.patch("/{id}")
def toggle_model(id: str, body: dict = Body(default={}), db: Session = Depends(get_db)):
    model_id = parse_int(id)
    if model_id is None:
        return invalid_id("Invalid model id")

    model = db.get(ModelRow, model_id)
    if model is None:
        return not_found("Model not found")

    if not isinstance(body.get("enabled"), bool):
        return JSONResponse({"error": "enabled (boolean) is required"}, status_code=400)

    model.enabled = body["enabled"]
    db.commit()

    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Probe helpers — extract the assistant's reply text from each wire format
# --------------------------------------------------------------------------- #

def _openai_reply(data: dict) -> str:
    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):  # some gateways return content parts
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        return str(text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _gemini_reply(data: dict) -> str:
    try:
        cand = (data.get("candidates") or [{}])[0]
        parts = ((cand.get("content") or {}).get("parts")) or []
        return " ".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
    except Exception:  # noqa: BLE001
        return ""


def _anthropic_reply(data: dict) -> str:
    try:
        blocks = data.get("content") or []
        return " ".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict)).strip()
    except Exception:  # noqa: BLE001
        return ""


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text[:60] + ("…" if len(text) > 60 else "")


# --------------------------------------------------------------------------- #
# POST /api/admin/models/{id}/ping — health-check a single model by asking it
# a REAL one-shot question. A plain GET /models ping only proves the endpoint
# is reachable; this proves the model is RESPONSIVE:
#   builtin     → real RouterX Free engine completion (measured latency)
#   gemini      → POST {base}/models/{model}:generateContent?key=…
#   anthropic   → POST {base}/v1/messages (x-api-key + version)
#   others      → POST {base}/chat/completions (OpenAI-compatible, Bearer)
# Healthy requires a 2xx AND non-empty reply text. HTTP 429 → cooling.
# Persists status / latencyMs / checkedAt / httpStatus on this model row only.
# --------------------------------------------------------------------------- #

@router.post("/{id}/ping")
async def ping_model(id: str):
    model_id = parse_int(id)
    if model_id is None:
        return invalid_id("Invalid model id")

    with SessionLocal() as db:
        model = db.scalars(
            select(ModelRow).where(ModelRow.id == model_id).options(joinedload(ModelRow.provider))
        ).first()
        if model is None:
            return not_found("Model not found")
        if model.provider is not None and model.provider.kind == "exchange":
            return JSONResponse({
                "error": "Exchange providers do not serve chat models",
                "status": "unsupported",
            }, status_code=400)

        provider = model.provider
        keys = db.scalars(
            select(ProviderKey).where(ProviderKey.providerId == provider.id).order_by(ProviderKey.id)
        ).all()
        question = random.choice(PROBE_QUESTIONS)

        if provider.kind == "builtin":
            # Real engine completion — the model must actually answer.
            started = time.monotonic()
            try:
                res = await nova_engine.chat(
                    [{"role": "user", "content": question}], timeout=PING_TIMEOUT_S
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                reply = _openai_reply(res)
                if reply:
                    ok, status, http_status = True, "healthy", 200
                    detail = f'engine replied "{_snippet(reply)}" in {latency_ms}ms'
                else:
                    ok, status, http_status = False, "dead", 200
                    detail = "engine reachable but the model gave no reply content — unresponsive"
            except Exception as err:  # noqa: BLE001
                latency_ms = int((time.monotonic() - started) * 1000)
                ok, status, http_status = False, "dead", 0
                detail = (str(err) or "engine unavailable")[:120]

        elif not keys or not provider.baseUrl:
            ok = False
            status = "unknown"
            latency_ms = 0
            http_status = 0
            detail = "No upstream key configured" if not keys else "Provider has no base URL configured"

        else:
            key = next((k.apiKey for k in keys if k.enabled), keys[0].apiKey)
            base = provider.baseUrl.rstrip("/")
            headers: dict = {"Content-Type": "application/json"}
            url = f"{base}/chat/completions"
            payload: dict = {
                "model": model.modelId,
                "messages": [{"role": "user", "content": question}],
                "max_tokens": 16,
                "temperature": 0,
                "stream": False,
            }
            extract = _openai_reply
            if provider.key == "gemini" or provider.kind == "gemini":
                url = f"{base}/models/{model.modelId}:generateContent?key={key}"
                payload = {
                    "contents": [{"parts": [{"text": question}]}],
                    "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
                }
                extract = _gemini_reply
            elif provider.kind == "anthropic":
                url = (f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages")
                headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
                payload = {
                    "model": model.modelId,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": question}],
                }
                extract = _anthropic_reply
            else:
                if provider.key not in ("openrouter",) or key:
                    headers["Authorization"] = f"Bearer {key}"

            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=PING_TIMEOUT_S) as client:
                    res = await client.post(url, json=payload, headers=headers)
                latency_ms = int((time.monotonic() - started) * 1000)
                http_status = res.status_code
                if 200 <= res.status_code < 300:
                    reply = extract(_safe_json(res))
                    if reply:
                        ok, status = True, "healthy"
                        detail = f'replied "{_snippet(reply)}" (HTTP {res.status_code}, {latency_ms}ms)'
                    else:
                        ok, status = False, "dead"
                        detail = f"reachable (HTTP {res.status_code}) but no reply content — unresponsive"
                elif res.status_code == 429:
                    ok, status = False, "cooling"
                    detail = "rate limited (HTTP 429) — cooling down"
                elif res.status_code in (401, 403):
                    ok, status = False, "dead"
                    detail = f"auth rejected (HTTP {res.status_code}) — key invalid or expired"
                elif res.status_code == 404:
                    ok, status = False, "dead"
                    detail = "model not found upstream (HTTP 404) — catalogue may be stale"
                else:
                    ok, status = False, "dead"
                    detail = f"upstream returned HTTP {res.status_code}"
            except Exception as err:  # noqa: BLE001 — network errors mark the model dead
                latency_ms = int((time.monotonic() - started) * 1000)
                ok, status, http_status = False, "dead", 0
                detail = (str(err) or "Network error")[:120]

        model.status = status
        model.latencyMs = latency_ms
        model.httpStatus = http_status
        model.checkedAt = utcnow()
        db.commit()

        return JSONResponse({
            "ok": ok,
            "status": status,
            "latency_ms": latency_ms,
            "http_status": http_status,
            "detail": detail,
        })


def _safe_json(res: httpx.Response) -> dict:
    try:
        data = res.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
