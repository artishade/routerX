"""Admin provider CRUD + real connectivity probes.

Python port of:
  src/app/api/admin/providers/route.ts           (GET list + POST create)
  src/app/api/admin/providers/presets/route.ts   (GET preset catalogue)
  src/app/api/admin/providers/[id]/route.ts      (PATCH + DELETE)
  src/app/api/admin/providers/[id]/test/route.ts (POST — real upstream probe)
  src/app/api/admin/providers/[id]/signin/route.ts   (POST)
  src/app/api/admin/providers/[id]/signout/route.ts  (POST)

Mounted at /api/admin/providers by main.py.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from nova import engine as nova_engine
from nova import synclog
from nova.config import slugify
from nova.database import SessionLocal, get_db
from nova.models import Model as ModelRow, Provider, ProviderKey, ProviderSession, utcnow
from nova.syncengine import sync_provider
from nova.provider_transport import anthropic_headers, anthropic_url

from ._common import as_num, as_str, epoch_ms, invalid_id, json_body, mask_key, not_found, parse_int

router = APIRouter()

FETCH_TIMEOUT_S = 8.0
FETCH_TIMEOUT_MS = 8000
ENGINE_PING_TIMEOUT_S = 20.0
KEYLESS_PROVIDERS = {"openrouter", "ollama"}
PROVIDER_KINDS = {"openai", "anthropic", "gemini", "builtin", "exchange"}


def validate_base_url(url: str, kind: str) -> str | None:
    """Require an API base URL, never an endpoint or a URL embedding credentials."""
    if kind == "builtin":
        return None if url == "internal://nova-engine" else "Invalid built-in engine URL"
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("https", "http") or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or any(c.isspace() for c in url)):
            return "Provide an HTTP(S) API base URL without credentials, query parameters or fragments"
        suffixes = ("/models", "/chat/completions", "/messages", "/embeddings")
        if kind == "exchange":
            suffixes += ("/quote", "/positions", "/balances", "/ticker")
        if parts.path.rstrip("/").endswith(suffixes):
            return "Provide the API base URL, not a specific endpoint"
    except ValueError:
        return "Invalid API base URL"
    return None

# --------------------------------------------------------------------------- #
# Built-in provider catalogue — kept in sync with GET /api/admin/meta.
# --------------------------------------------------------------------------- #

PRESETS: list[dict] = [
    {
        "key": "novafree", "name": "RouterX Free Engine", "kind": "builtin",
        "base_url": "internal://nova-engine", "prefix": "nova/", "key_hint": "no key needed",
        "free_tier": "Built-in — every model here is free, no key required",
        "docs_url": "https://github.com/artishade/Novarouter", "auth_url": None,
        "requires_signin": False, "color": "#10b981", "priority": 1,
    },
    {
        "key": "openrouter", "name": "OpenRouter", "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1", "prefix": "openrouter/", "key_hint": "sk-or-v1-…",
        "free_tier": "Dozens of :free models, 200 req/day without credits",
        "docs_url": "https://openrouter.ai/docs", "auth_url": "https://openrouter.ai/settings/keys",
        "requires_signin": True, "color": "#8b5cf6", "priority": 10,
    },
    {
        "key": "groq", "name": "Groq Cloud", "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1", "prefix": "groq/", "key_hint": "gsk_…",
        "free_tier": "Free tier: 30 req/min, 14,400 req/day",
        "docs_url": "https://console.groq.com/docs", "auth_url": "https://console.groq.com/keys",
        "requires_signin": True, "color": "#f97316", "priority": 10,
    },
    {
        "key": "gemini", "name": "Google AI Studio", "kind": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta", "prefix": "gemini/", "key_hint": "AIza…",
        "free_tier": "Gemini API free tier: 15 RPM, 1,500 req/day (Flash)",
        "docs_url": "https://ai.google.dev/docs", "auth_url": "https://aistudio.google.com/app/apikey",
        "requires_signin": True, "color": "#22c55e", "priority": 15,
    },
    {
        "key": "cerebras", "name": "Cerebras Inference", "kind": "openai",
        "base_url": "https://api.cerebras.ai/v1", "prefix": "cerebras/", "key_hint": "csk-…",
        "free_tier": "Free tier: ~1M tokens/day at 2,000+ tok/s",
        "docs_url": "https://inference-docs.cerebras.ai", "auth_url": "https://cloud.cerebras.ai",
        "requires_signin": True, "color": "#f59e0b", "priority": 20,
    },
    {
        "key": "github-models", "name": "GitHub Models", "kind": "openai",
        "base_url": "https://models.inference.ai.azure.com", "prefix": "github/", "key_hint": "ghp_… / github_pat_…",
        "free_tier": "Free with any GitHub account — GPT-4o, Llama, Phi, DeepSeek",
        "docs_url": "https://docs.github.com/en/github-models", "auth_url": "https://github.com/settings/tokens",
        "requires_signin": True, "color": "#e2e8f0", "priority": 25,
    },
    {
        "key": "mistral", "name": "Mistral La Plateforme", "kind": "openai",
        "base_url": "https://api.mistral.ai/v1", "prefix": "mistral/", "key_hint": "32-char token",
        "free_tier": "Free experiment plan: 1 req/s, 500k tokens/min",
        "docs_url": "https://docs.mistral.ai", "auth_url": "https://console.mistral.ai/api-keys",
        "requires_signin": True, "color": "#fb923c", "priority": 30,
    },
    {
        "key": "nvidia", "name": "NVIDIA NIM", "kind": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1", "prefix": "nvidia/", "key_hint": "nvapi-…",
        "free_tier": "1,000 free credits on signup for hosted NIM endpoints",
        "docs_url": "https://docs.api.nvidia.com", "auth_url": "https://build.nvidia.com",
        "requires_signin": True, "color": "#76b900", "priority": 35,
    },
    {
        "key": "deepseek", "name": "DeepSeek API", "kind": "openai",
        "base_url": "https://api.deepseek.com/v1", "prefix": "deepseek/", "key_hint": "sk-…",
        "free_tier": "Pay-as-you-go — new accounts receive trial credits",
        "docs_url": "https://api-docs.deepseek.com", "auth_url": "https://platform.deepseek.com/api_keys",
        "requires_signin": True, "color": "#64748b", "priority": 40,
    },
    {
        "key": "together", "name": "Together AI", "kind": "openai",
        "base_url": "https://api.together.xyz/v1", "prefix": "together/", "key_hint": "64-char token",
        "free_tier": "$1 free credit on signup",
        "docs_url": "https://docs.together.ai", "auth_url": "https://api.together.ai/settings/api-keys",
        "requires_signin": True, "color": "#0f766e", "priority": 45,
    },
    {
        "key": "xai", "name": "xAI Grok", "kind": "openai",
        "base_url": "https://api.x.ai/v1", "prefix": "xai/", "key_hint": "xai-…",
        "free_tier": None,
        "docs_url": "https://docs.x.ai", "auth_url": "https://console.x.ai",
        "requires_signin": True, "color": "#e5e7eb", "priority": 50,
    },
    {
        "key": "fireworks", "name": "Fireworks AI", "kind": "openai",
        "base_url": "https://api.fireworks.ai/inference/v1", "prefix": "fireworks/", "key_hint": "fw_…",
        "free_tier": "$1 free credits + serverless free tier for small models",
        "docs_url": "https://docs.fireworks.ai", "auth_url": "https://fireworks.ai/account/api-keys",
        "requires_signin": True, "color": "#fb7185", "priority": 55,
    },
    {
        "key": "ollama", "name": "Ollama (local)", "kind": "openai",
        "base_url": "http://localhost:11434/v1", "prefix": "ollama/", "key_hint": "not required",
        "free_tier": "100% free — runs on your machine",
        "docs_url": "https://github.com/ollama/ollama", "auth_url": None,
        "requires_signin": False, "color": "#94a3b8", "priority": 80,
    },
    {
        "key": "openai", "name": "OpenAI", "kind": "openai",
        "base_url": "https://api.openai.com/v1", "prefix": "openai/", "key_hint": "sk-…",
        "free_tier": None,
        "docs_url": "https://platform.openai.com/docs", "auth_url": "https://platform.openai.com/api-keys",
        "requires_signin": True, "color": "#0ea36e", "priority": 90,
    },
    {
        "key": "custom-gateway", "name": "Custom OpenAI-compatible gateway", "kind": "openai",
        "base_url": "", "prefix": "gateway/", "key_hint": "Gateway API key",
        "free_tier": None, "docs_url": None, "auth_url": None,
        "requires_signin": False, "color": "#38bdf8", "priority": 95,
    },
    {
        "key": "custom-exchange", "name": "Exchange (read-only)", "kind": "exchange",
        "base_url": "", "prefix": "exchange/", "key_hint": "API key + secret (JSON or key:secret)",
        "free_tier": None, "docs_url": None, "auth_url": None,
        "requires_signin": False, "color": "#f59e0b", "priority": 95,
    },
    {
        "key": "cloudflare-worker", "name": "Cloudflare Worker (OpenAI-compatible)", "kind": "openai",
        "base_url": "", "prefix": "worker/", "key_hint": "Worker bearer token",
        "free_tier": None, "docs_url": "https://developers.cloudflare.com/workers/", "auth_url": None,
        "requires_signin": False, "color": "#f48120", "priority": 95,
    },
    {
        "key": "anthropic", "name": "Anthropic", "kind": "anthropic",
        "base_url": "https://api.anthropic.com", "prefix": "anthropic/", "key_hint": "sk-ant-…",
        "free_tier": None,
        "docs_url": "https://docs.anthropic.com", "auth_url": "https://console.anthropic.com/settings/keys",
        "requires_signin": True, "color": "#d97757", "priority": 90,
    },
]


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #

def to_session(s: ProviderSession) -> dict:
    return {
        "username": s.username,
        "display_name": s.displayName,
        "plan": s.plan,
        "status": s.status,
        "token_mask": s.tokenMask,
        "connected_at": epoch_ms(s.connectedAt),
    }


def to_provider(p: Provider, key_count: int, model_counts: tuple[int, int, int], session: ProviderSession | None) -> dict:
    return {
        "id": p.id,
        "key": p.key,
        "name": p.name,
        "kind": p.kind,
        "base_url": p.baseUrl,
        "prefix": p.prefix,
        "enabled": p.enabled,
        "priority": p.priority,
        "color": p.color,
        "docs_url": p.docsUrl,
        "auth_url": p.authUrl,
        "requires_auth": p.requiresAuth,
        "free_tier": p.freeTier,
        "created_at": epoch_ms(p.createdAt),
        "key_count": key_count,
        "model_count": model_counts[0],
        "ok_count": model_counts[1],
        "cooling_count": model_counts[2],
        "session": to_session(session) if session is not None else None,
    }


def _base36(n: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = alphabet[r] + out
    return out


# --------------------------------------------------------------------------- #
# GET /api/admin/providers — all providers with counts + latest session
# --------------------------------------------------------------------------- #

@router.get("")
def list_providers(db: Session = Depends(get_db)):
    providers = db.scalars(
        select(Provider).order_by(Provider.priority.asc(), Provider.id.asc())
    ).all()

    # Avoid loading all key secrets, model descriptions/capabilities and full ORM
    # objects for a list that only displays three model counts and a key count.
    key_counts = dict(db.execute(
        select(ProviderKey.providerId, func.count(ProviderKey.id))
        .group_by(ProviderKey.providerId)
    ).all())
    model_counts = {
        provider_id: (int(total), int(healthy), int(cooling))
        for provider_id, total, healthy, cooling in db.execute(
            select(
                ModelRow.providerId,
                func.count(ModelRow.id),
                func.sum(case((ModelRow.status == "healthy", 1), else_=0)),
                func.sum(case((ModelRow.status == "cooling", 1), else_=0)),
            ).group_by(ModelRow.providerId)
        ).all()
    }

    ranked = select(
        ProviderSession.id.label("session_id"),
        func.row_number().over(
            partition_by=ProviderSession.providerKey,
            order_by=(ProviderSession.connectedAt.desc(), ProviderSession.id.desc()),
        ).label("rank"),
    ).subquery()
    sessions = db.scalars(
        select(ProviderSession).join(ranked, ProviderSession.id == ranked.c.session_id)
        .where(ranked.c.rank == 1)
    ).all()
    latest = {s.providerKey: s for s in sessions}

    return JSONResponse([
        to_provider(p, int(key_counts.get(p.id, 0)), model_counts.get(p.id, (0, 0, 0)), latest.get(p.key))
        for p in providers
    ])


# --------------------------------------------------------------------------- #
# GET /api/admin/providers/presets — built-in provider preset catalogue
# --------------------------------------------------------------------------- #

@router.get("/presets")
def list_presets():
    return JSONResponse(PRESETS)


# --------------------------------------------------------------------------- #
# Live sync jobs — background catalogue discovery with SSE-streamed progress
# lines for the Add-Provider dialog.
# --------------------------------------------------------------------------- #

def _start_sync_job(provider: Provider) -> str:
    """Run sync_provider in a background thread, streaming every step into a
    synclog job the browser can follow over Server-Sent Events."""
    jid = synclog.create_job(f"discover live catalogue for “{provider.name}”")

    def runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)

            def log(msg: str) -> None:
                synclog.log_line(jid, msg)

            with SessionLocal() as bdb:
                report = loop.run_until_complete(sync_provider(bdb, {
                    "id": provider.id, "key": provider.key, "name": provider.name,
                    "kind": provider.kind, "baseUrl": provider.baseUrl,
                    "prefix": provider.prefix,
                }, log=log))
            ok = bool(report.get("ok"))
            if ok:
                summary = (
                    f"{report.get('created', 0)} new · {report.get('updated', 0)} refreshed · "
                    f"{report.get('discovered', 0)} discovered ({report.get('duration_ms', 0)}ms)"
                )
            else:
                summary = f"failed — {report.get('error') or 'discovery failed'}"
            synclog.finish_job(jid, ok, summary)
        except Exception as err:  # noqa: BLE001 — the job must always finish
            synclog.finish_job(jid, False, f"failed — {str(err)[:160]}")
        finally:
            loop.close()

    threading.Thread(target=runner, daemon=True, name=f"nova-sync-{provider.key}").start()
    return jid


@router.get("/sync-logs/stream")
async def sync_logs_stream(req: Request):
    """SSE stream of a sync job's live lines; ends with an `done` event."""
    jid = req.query_params.get("job") or ""
    job = synclog.get_job(jid)
    if job is None:
        return JSONResponse({"error": "unknown or expired sync job"}, status_code=404)

    async def gen():
        sent = 0
        yield ": nova-sync-stream\n\n"
        while True:
            lines = synclog.job_lines(jid)
            while sent < len(lines):
                yield f"data: {json_dumps(lines[sent])}\n\n"
                sent += 1
            job = synclog.get_job(jid)
            if job is None:
                return
            if job["done"] and sent >= len(lines):
                yield "event: done\n"
                yield f"data: {json_dumps({'ok': job['ok'], 'summary': job['summary']})}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# POST /api/admin/providers — create a provider (reuses preset data when the
# name matches) + auto-sync its live model catalogue.
#   ?logs=1 → the sync runs as a background job and the response returns
#   immediately with a `stream` SSE URL the dashboard follows for live logs.
# --------------------------------------------------------------------------- #

@router.post("")
async def create_provider(req: Request):
    body = await json_body(req)

    name = body["name"].strip() if isinstance(body.get("name"), str) else ""
    if not name:
        return JSONResponse({"error": "Provider name is required"}, status_code=400)

    # Reuse preset data when the name matches (case-insensitive) or the slug matches a preset key.
    slug = slugify(name)
    preset = next((p for p in PRESETS if p["name"].lower() == name.lower()), None)
    if preset is None:
        preset = next((p for p in PRESETS if p["key"] == slug), None)

    with SessionLocal() as db:
        # Generate a unique key slug from the name (prefer the preset key when available).
        key = preset["key"] if preset else slug
        suffix = 2
        while db.scalar(select(Provider.id).where(Provider.key == key)) is not None:
            key = f"{slug}-{suffix}"
            suffix += 1
            if suffix > 50:
                key = f"{slug}-{_base36(int(time.time() * 1000))}"
                break

        kind = as_str(body.get("kind"))
        if kind is None:
            kind = preset["kind"] if preset else "openai"
        base_url = as_str(body.get("base_url"))
        if base_url is None:
            base_url = preset["base_url"] if preset else ""
        if kind not in PROVIDER_KINDS:
            return JSONResponse({"error": "Unsupported provider kind"}, status_code=400)
        base_url = base_url.strip().rstrip("/")
        url_error = validate_base_url(base_url, kind)
        if url_error:
            return JSONResponse({"error": url_error}, status_code=400)
        prefix = as_str(body.get("prefix"))
        if prefix is None:
            prefix = preset["prefix"] if preset else f"{slug}/"
        priority = as_num(body.get("priority"))
        if priority is None:
            priority = preset["priority"] if preset else 100
        docs_url = as_str(body.get("docs_url"))
        if docs_url is None:
            docs_url = preset["docs_url"] if preset else None
        auth_url = as_str(body.get("auth_url"))
        if auth_url is None:
            auth_url = preset["auth_url"] if preset else None
        free_tier = as_str(body.get("free_tier"))
        if free_tier is None:
            free_tier = preset["free_tier"] if preset else None

        created = Provider(
            key=key,
            name=preset["name"] if preset else name,
            kind=kind,
            baseUrl=base_url,
            prefix=prefix,
            priority=int(round(priority)),
            color=preset["color"] if preset else "#10b981",
            docsUrl=docs_url,
            authUrl=auth_url,
            requiresAuth=auth_url is not None,
            freeTier=free_tier,
        )
        db.add(created)
        db.flush()

        # Optional multiline/comma-separated key list.
        api_keys_raw = as_str(body.get("api_keys")) or ""
        keys = [s.strip() for s in re.split(r"[\n,]+", api_keys_raw) if s.strip()]
        if keys:
            db.add_all([
                ProviderKey(
                    providerId=created.id,
                    label="key" if len(keys) == 1 else f"key-{i + 1}",
                    apiKey=api_key,
                )
                for i, api_key in enumerate(keys)
            ])
        db.commit()

        # Exchange providers have no model catalogue: never run discovery for them.
        if created.kind == "exchange":
            return JSONResponse({
                "id": created.id,
                "keys_added": len(keys),
                "models_added": 0,
                "sync_skipped": "exchange providers do not expose a model catalogue",
            }, status_code=200)

        # Auto-sync the new provider's live model catalogue so it is usable immediately.
        # Best-effort: a discovery failure must not fail the creation — the dashboard
        # "Sync models" button can retry, and the error is surfaced honestly.
        if req.query_params.get("logs") not in (None, "", "0"):
            # Live-logs mode: create the row, discover in the background, and let
            # the dialog follow the SSE stream for real-time progress.
            jid = _start_sync_job(created)
            return JSONResponse({
                "id": created.id,
                "keys_added": len(keys),
                "job": jid,
                "stream": f"/api/admin/providers/sync-logs/stream?job={jid}",
            }, status_code=200)

        models_added = 0
        sync_error: str | None = None
        try:
            report = await sync_provider(db, {
                "id": created.id, "key": created.key, "name": created.name,
                "kind": created.kind, "baseUrl": created.baseUrl, "prefix": created.prefix,
            })
            models_added = report.get("created", 0)
            if not report.get("ok"):
                sync_error = report.get("error") or "model discovery failed"
        except Exception as err:  # noqa: BLE001 — surfaced honestly, never fails creation
            sync_error = str(err) or "model discovery failed"

        payload: dict[str, Any] = {
            "id": created.id,
            "keys_added": len(keys),
            "models_added": models_added,
        }
        if sync_error:
            payload["sync_error"] = sync_error
        return JSONResponse(payload, status_code=200)


# --------------------------------------------------------------------------- #
# PATCH /api/admin/providers/{id} — update editable provider fields
# --------------------------------------------------------------------------- #

@router.patch("/{id}")
def update_provider(id: str, body: dict = Body(default={}), db: Session = Depends(get_db)):
    provider_id = parse_int(id)
    if provider_id is None:
        return invalid_id("Invalid provider id")

    provider = db.get(Provider, provider_id)
    if provider is None:
        return not_found("Provider not found")

    if isinstance(body.get("enabled"), bool):
        provider.enabled = body["enabled"]
    if body.get("priority") is not None:
        n = as_num(body.get("priority"))
        if n is not None:
            provider.priority = int(round(n))
    if isinstance(body.get("base_url"), str):
        base_url = body["base_url"].strip().rstrip("/")
        url_error = validate_base_url(base_url, provider.kind)
        if url_error:
            return JSONResponse({"error": url_error}, status_code=400)
        provider.baseUrl = base_url
    if isinstance(body.get("prefix"), str):
        provider.prefix = body["prefix"]
    if isinstance(body.get("name"), str) and body["name"].strip() != "":
        provider.name = body["name"].strip()

    db.commit()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# DELETE /api/admin/providers/{id} — removes provider + cascades keys/models
# --------------------------------------------------------------------------- #

@router.delete("/{id}")
def delete_provider(id: str, db: Session = Depends(get_db)):
    provider_id = parse_int(id)
    if provider_id is None:
        return invalid_id("Invalid provider id")

    provider = db.get(Provider, provider_id)
    if provider is None:
        return not_found("Provider not found")
    if provider.key == "novafree":
        return JSONResponse({"error": "The built-in RouterX Free engine cannot be deleted"}, status_code=400)

    db.delete(provider)
    db.query(ProviderSession).filter(ProviderSession.providerKey == provider.key).delete(synchronize_session=False)
    db.commit()

    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# POST /api/admin/providers/{id}/test — REAL provider connectivity test
# --------------------------------------------------------------------------- #

def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _extract_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "models", "body"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


async def probe_upstream(provider_key: str, kind: str, base_url: str, api_key: str | None) -> dict:
    """Real GET against one upstream key, using the provider's wire format."""
    from urllib.parse import quote

    started = time.monotonic()
    if kind == "exchange":
        # Exchange credentials are verified through the trading namespace, not a
        # model catalogue: never probe {base}/models for a read-only exchange.
        return {
            "ok": True, "http_status": 0, "latency_ms": 0, "models_found": 0,
            "detail": "Exchange provider configured — verify read access with /v1/trading/quote",
        }
    url = _join_url(base_url, "models")
    headers: dict[str, str] = {"Accept": "application/json"}

    if kind == "gemini":
        if not api_key:
            raise ValueError("missing key")
        url = _join_url(base_url, f"models?key={quote(api_key, safe='')}&pageSize=50")
    elif kind == "anthropic":
        if not api_key:
            raise ValueError("missing key")
        url = anthropic_url(base_url, "models?limit=50")
        headers.update(anthropic_headers(api_key))
    elif provider_key == "openrouter" and api_key:
        # OpenRouter's /models catalogue is public and accepts any Bearer — /auth/key
        # actually validates the key against the account.
        url = _join_url(base_url, "auth/key")
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
            res = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return {
            "ok": False, "http_status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000), "models_found": 0,
            "detail": f"timed out after {FETCH_TIMEOUT_MS}ms",
        }
    except httpx.RequestError:
        latency = int((time.monotonic() - started) * 1000)
        return {
            "ok": False, "http_status": 0, "latency_ms": latency, "models_found": 0,
            "detail": "upstream connection failed",
        }

    latency = int((time.monotonic() - started) * 1000)
    if 200 <= res.status_code < 300:
        count = 0
        detail = f"Upstream catalogue reachable — HTTP {res.status_code} in {latency}ms"
        try:
            data = res.json()
            count = len(_extract_list(data))
            if url.endswith("/auth/key"):
                detail = f"Key authenticated with OpenRouter — HTTP {res.status_code} in {latency}ms"
        except Exception:
            count = 0
        return {"ok": True, "http_status": res.status_code, "latency_ms": latency,
                "models_found": count, "detail": detail}

    detail = (
        f"auth rejected (HTTP {res.status_code})"
        if res.status_code in (401, 403)
        else f"upstream returned HTTP {res.status_code}"
    )
    return {"ok": False, "http_status": res.status_code, "latency_ms": latency,
            "models_found": 0, "detail": detail}


async def probe_nova_engine() -> dict:
    """Real end-to-end ping of the built-in RouterX Free engine (free-model sidecar)."""
    started = time.monotonic()
    try:
        completion = await nova_engine.chat(
            [{"role": "user", "content": "Reply with the single word: pong"}],
            timeout=ENGINE_PING_TIMEOUT_S,
        )
        content: Any = None
        try:
            content = completion["choices"][0]["message"]["content"]
        except (KeyError, TypeError, IndexError):
            content = None
        latency = int((time.monotonic() - started) * 1000)
        ok = isinstance(content, str) and len(content) > 0
        return {
            "ok": ok, "http_status": 200, "latency_ms": latency, "models_found": 0,
            "detail": f"Engine answered a live test completion in {latency}ms"
            if ok else "Engine returned an empty completion",
        }
    except Exception as err:  # noqa: BLE001
        msg = str(err)[:200]
        return {
            "ok": False, "http_status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000), "models_found": 0,
            "detail": f"Built-in engine unavailable: {msg}",
        }


@router.post("/{id}/test")
async def test_provider(id: str):
    provider_id = parse_int(id)
    if provider_id is None:
        return invalid_id("Invalid provider id")

    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        if provider is None:
            return not_found("Provider not found")

        enabled_keys = db.scalars(
            select(ProviderKey)
            .where(ProviderKey.providerId == provider_id, ProviderKey.enabled.is_(True))
            .order_by(ProviderKey.weight.desc(), ProviderKey.id)
        ).all()

        if provider.kind == "builtin":
            engine = await probe_nova_engine()
            models = db.scalar(
                select(func.count())
                .select_from(ModelRow)
                .where(ModelRow.providerId == provider.id, ModelRow.enabled.is_(True))
            ) or 0
            return JSONResponse({
                "ok": engine["ok"],
                "provider": provider.key,
                "key_count": 0,
                "latency_ms": engine["latency_ms"],
                "status": "ok" if engine["ok"] else "engine_error",
                "models_found": models,
                "message": f"{engine['detail']} · {models} engine model(s) enabled"
                if engine["ok"] else engine["detail"],
            })

        if not provider.baseUrl or provider.baseUrl.startswith("internal://"):
            return JSONResponse({
                "ok": False,
                "provider": provider.key,
                "key_count": len(enabled_keys),
                "latency_ms": 0,
                "status": "misconfigured",
                "models_found": 0,
                "message": "Provider has no upstream base URL configured — edit it before testing",
            })

        if provider.kind == "exchange":
            return JSONResponse({
                "ok": True,
                "provider": provider.key,
                "key_count": len(enabled_keys),
                "latency_ms": 0,
                "status": "ok",
                "models_found": 0,
                "message": "Exchange provider configured — no model catalogue is probed; verify read access with /v1/trading/quote",
            })

        if not enabled_keys and provider.key not in KEYLESS_PROVIDERS:
            return JSONResponse({
                "ok": False,
                "provider": provider.key,
                "key_count": 0,
                "latency_ms": 0,
                "status": "no_key",
                "models_found": 0,
                "message": "No enabled API key — add at least one key, then test again",
            })

        # Probe up to 3 enabled keys (sequential, bounded) — any success means the provider works.
        # Keyless providers (openrouter public catalogue, local ollama) are probed without a key.
        candidates: list[ProviderKey | None] = list(enabled_keys[:3])
        if not candidates:
            candidates = [None]
        probes: list[dict] = []
        for k in candidates:
            try:
                result = await probe_upstream(
                    provider.key, provider.kind, provider.baseUrl, k.apiKey if k else None
                )
            except Exception as err:  # noqa: BLE001 — unreachable in practice (missing key)
                result = {"ok": False, "http_status": 0, "latency_ms": 0,
                          "models_found": 0, "detail": str(err)[:160]}
            probes.append(result)
            if probes[-1]["ok"]:
                break  # first working key is enough

        best = next((p for p in probes if p["ok"]), probes[-1] if probes else {
            "ok": False, "http_status": 0, "latency_ms": 0, "models_found": 0,
            "detail": "no enabled key to probe",
        })

        if best["ok"]:
            status = "ok"
        elif best["http_status"] in (401, 403):
            status = "auth_error"
        elif best["http_status"] > 0:
            status = "http_error"
        else:
            status = "network_error"

        failed_keys = sum(1 for p in probes if not p["ok"])
        if best["ok"]:
            message = (
                f"{best['detail']} · {best['models_found']} models visible upstream (public catalogue — no key required)"
                if not enabled_keys
                else f"{best['detail']} · {best['models_found']} models visible upstream"
            )
        else:
            message = best["detail"] + (f" ({failed_keys} key(s) probed)" if failed_keys > 1 else "")

        return JSONResponse({
            "ok": best["ok"],
            "provider": provider.key,
            "key_count": len(enabled_keys),
            "latency_ms": best["latency_ms"],
            "status": status,
            "models_found": best["models_found"],
            "message": message,
        })


# --------------------------------------------------------------------------- #
# POST /api/admin/providers/{id}/signin — create/refresh the provider session
# --------------------------------------------------------------------------- #

@router.post("/{id}/signin")
async def signin_provider(id: str, req: Request):
    provider_id = parse_int(id)
    if provider_id is None:
        return invalid_id("Invalid provider id")

    body = await json_body(req)
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        if provider is None:
            return not_found("Provider not found")
        if provider.authUrl is None:
            return JSONResponse({"error": "This provider does not require sign-in"}, status_code=400)

        username = ""
        u = body.get("username")
        if isinstance(u, str) and u.strip() != "":
            username = u.strip()
        if not username:
            username = f"{provider.key}-operator"

        newest_key = db.scalars(
            select(ProviderKey)
            .where(ProviderKey.providerId == provider.id)
            .order_by(ProviderKey.createdAt.desc(), ProviderKey.id.desc())
        ).first()
        token_mask = mask_key(newest_key.apiKey) if newest_key else "pat-••••"

        db.query(ProviderSession).filter(ProviderSession.providerKey == provider.key).delete(synchronize_session=False)
        session = ProviderSession(
            providerKey=provider.key,
            username=username,
            displayName=provider.name,
            plan="free",
            status="connected",
            tokenMask=token_mask,
            connectedAt=utcnow(),
        )
        db.add(session)
        db.commit()

        return JSONResponse({"ok": True, "session": to_session(session)})


# --------------------------------------------------------------------------- #
# POST /api/admin/providers/{id}/signout — drop the provider session
# --------------------------------------------------------------------------- #

@router.post("/{id}/signout")
def signout_provider(id: str, db: Session = Depends(get_db)):
    provider_id = parse_int(id)
    if provider_id is None:
        return invalid_id("Invalid provider id")

    provider = db.get(Provider, provider_id)
    if provider is None:
        return not_found("Provider not found")

    db.query(ProviderSession).filter(ProviderSession.providerKey == provider.key).delete(synchronize_session=False)
    db.commit()

    return JSONResponse({"ok": True})
