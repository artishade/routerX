"""Admin misc routes — port of:
  src/app/api/admin/meta/route.ts       → GET /api/admin/meta
  src/app/api/admin/stats/route.ts      → GET /api/admin/stats
  src/app/api/admin/analytics/route.ts  → GET /api/admin/analytics
  src/app/api/admin/logs/route.ts       → GET /api/admin/logs

Included by main.py directly under the /api/admin prefix (no local prefix).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from nova.config import public_base_url
from nova.database import get_db
from nova.kv import get_config
from nova.models import (
    Model as ModelRow,
    Provider,
    ProviderKey,
    ProviderSession,
    RequestLog,
    SystemConfig,
)
from .admin_providers import PRESETS as PROVIDER_PRESETS

router = APIRouter()

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def now_ms() -> int:
    return int(time.time() * 1000)


def ts_ms(dt: datetime | None) -> int:
    """Prisma stored naive UTC datetimes → epoch ms."""
    if dt is None:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms → naive UTC datetime (Prisma storage convention)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def r1(n: Any) -> float:
    """Math.round(n * 10) / 10 parity.

    SQL numeric aggregates must survive the round trip: psycopg2 maps Postgres
    `numeric` to Decimal, and `Decimal + 0.5` raises TypeError, so `AVG()` on
    Postgres 500'd the stats endpoint while SQLite (which returns a float)
    looked fine. Coerce before rounding, and never emit NaN/Infinity (invalid
    JSON that would break the client).
    """
    try:
        value = float(n)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return math.floor(value * 10 + 0.5) / 10


def _num_or_none(raw: str | None) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _js_num(n: float) -> float | int:
    """JSON cleanliness parity with JS Number: 4096.0 → 4096."""
    return int(n) if n == int(n) else n


def cfg_map(db: Session, keys: list[str]) -> dict[str, str]:
    rows = db.scalars(select(SystemConfig).where(SystemConfig.key.in_(keys))).all()
    return {r.key: r.value for r in rows}


def _rss_kb() -> int:
    """Real process RSS: /proc/self/status VmRSS, ru_maxrss fallback."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    if kb:
                        return kb
    except OSError:
        pass
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)  # KB on Linux
    except Exception:
        return 0


def _vm_peak_kb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmPeak:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


# --------------------------------------------------------------------------- #
# GET /meta — gateway configuration descriptor
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
        "free_tier": "$25/month free credits while data sharing is enabled",
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
        "key": "anthropic", "name": "Anthropic", "kind": "anthropic",
        "base_url": "https://api.anthropic.com", "prefix": "anthropic/", "key_hint": "sk-ant-…",
        "free_tier": None,
        "docs_url": "https://docs.anthropic.com", "auth_url": "https://console.anthropic.com/settings/keys",
        "requires_signin": True, "color": "#d97757", "priority": 90,
    },
]


@router.get("/meta")
def meta(request: Request, db: Session = Depends(get_db)):
    # Requirement #2: the advertised base_url must point at the hosted domain —
    # env PUBLIC_BASE_URL wins, else the incoming request origin, else the
    # novarouter.onrender.com constant (nova.config.public_base_url).
    base = public_base_url(str(request.base_url))

    cfg = cfg_map(db, ["admin_token", "spoof_model", "auto_fallback", "hedge_delay", "cache_ttl"])

    def flag(key: str, dflt: bool) -> bool:
        v = cfg.get(key)
        if v is None:
            return dflt
        return v in ("1", "true")

    def num(key: str, dflt: float) -> float | int:
        v = cfg.get(key)
        if v is None:
            return dflt
        try:
            n = float(v)
        except ValueError:
            return dflt
        return _js_num(n) if math.isfinite(n) else dflt

    return {
        "version": "2.0.0",
        "base_url": f"{base}/v1",
        "storage": "hybrid (local + cloud)",
        "fallback": {
            "auto": flag("auto_fallback", True),
            "spoof_model": flag("spoof_model", True),
            "max": 3,
        },
        "cooldowns": {"429": 60, "402": 300, "5xx": 30},
        "scheduler": {"check_interval": 3600},
        "hedging": {"delay": num("hedge_delay", 2)},
        "cache": {"ttl": num("cache_ttl", 600), "max_entries": 1000},
        "limits": {"file_max_mb": 25, "batch_max_items": 50000},
        "presets": PROVIDER_PRESETS,
        "kinds": ["openai", "anthropic", "gemini", "builtin", "exchange"],
        "statuses": ["healthy", "cooling", "dead", "unknown"],
        "admin_token": cfg.get("admin_token") or "nova-admin-token",
    }


# --------------------------------------------------------------------------- #
# GET /stats — live gateway telemetry
# --------------------------------------------------------------------------- #


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    now = now_ms()
    day_ago = ms_to_dt(now - 24 * 3600_000)

    totals = db.execute(
        select(func.count(RequestLog.id), func.sum(RequestLog.tokensIn), func.sum(RequestLog.tokensOut))
    ).one()
    total_requests = int(totals[0] or 0)
    total_tokens_in = int(totals[1] or 0)
    total_tokens_out = int(totals[2] or 0)

    recent = (
        select(RequestLog.status, RequestLog.latencyMs, RequestLog.via)
        .where(RequestLog.ts >= day_ago).limit(10000)
        .subquery()
    )
    # Aggregate the bounded 24h window in SQL instead of transferring up to
    # 10,000 rows into Python on every tab navigation / live refresh.
    n24, cache_hits, errors, avg_latency = db.execute(select(
        func.count(),
        func.coalesce(func.sum(case((recent.c.via.in_(("cache", "cache-hit")), 1), else_=0)), 0),
        func.coalesce(func.sum(case((recent.c.status >= 400, 1), else_=0)), 0),
        func.coalesce(func.avg(recent.c.latencyMs), 0),
    ).select_from(recent)).one()

    active_keys = db.scalar(
        select(func.count()).select_from(ProviderKey).where(ProviderKey.enabled.is_(True))
    ) or 0
    active_models = db.scalar(
        select(func.count()).select_from(ModelRow).where(ModelRow.enabled.is_(True))
    ) or 0
    healthy_models = db.scalar(
        select(func.count()).select_from(ModelRow)
        .where(ModelRow.enabled.is_(True), ModelRow.status == "healthy")
    ) or 0
    dead_models = db.scalar(
        select(func.count()).select_from(ModelRow).where(ModelRow.status == "dead")
    ) or 0
    providers_count = db.scalar(select(func.count()).select_from(Provider)) or 0

    session_keys = list(db.scalars(select(ProviderSession.providerKey).distinct()).all())
    connected_providers = (
        db.scalar(select(func.count()).select_from(Provider).where(Provider.key.in_(session_keys)))
        if session_keys else 0
    ) or 0

    cfg = cfg_map(db, ["gateway_started_at", "v8_heap_mb", "swap_mb"])

    def cfg_num(key: str, dflt: int) -> int:
        raw = cfg.get(key)
        if raw is None:
            return dflt
        try:
            n = float(raw)
        except ValueError:
            return dflt
        return int(n) if math.isfinite(n) else dflt

    started_at = cfg_num("gateway_started_at", 0)
    uptime_s = max(0, (now - started_at) // 1000) if started_at > 0 else 0

    rss_k = _rss_kb()
    peak_k = _vm_peak_kb() or rss_k
    heap_used_mb = rss_k / 1024
    heap_total_mb = peak_k / 1024

    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens_in + total_tokens_out,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "active_keys": int(active_keys),
        "active_models": int(active_models),
        "dead_models": int(dead_models),
        "healthy_models": int(healthy_models),
        "providers_count": int(providers_count),
        "connected_providers": int(connected_providers),
        "cache_hit_rate": r1((cache_hits / n24) * 100) if n24 else 0,
        "avg_latency_ms": r1(avg_latency),
        "error_rate": r1((errors / n24) * 100) if n24 else 0,
        "uptime_s": int(uptime_s),
        "memory": {
            "heap_used_mb": r1(heap_used_mb),
            "heap_total_mb": r1(heap_total_mb),
            "rss_mb": r1(heap_used_mb),
            "pct": r1((heap_used_mb / heap_total_mb) * 100) if heap_total_mb > 0 else 0,
        },
        "v8": {
            "heap_mb": cfg_num("v8_heap_mb", 2048),
            "swap_mb": cfg_num("swap_mb", 2048),
        },
    }


# --------------------------------------------------------------------------- #
# GET /analytics — timeseries + top groupings (Asia/Dhaka bucketing parity)
# --------------------------------------------------------------------------- #

DHAKA_OFFSET_MS = 6 * 3600_000


def _hour_key(ts: int) -> int:
    return ((ts + DHAKA_OFFSET_MS) // 3600_000) * 3600_000 - DHAKA_OFFSET_MS


def _day_key(ts: int) -> int:
    return ((ts + DHAKA_OFFSET_MS) // 86400_000) * 86400_000 - DHAKA_OFFSET_MS


def _hour_label(key_ts: int) -> str:
    d = datetime.fromtimestamp((key_ts + DHAKA_OFFSET_MS) // 1000, tz=timezone.utc)
    return f"{d.hour:02d}:00"


def _day_label(key_ts: int) -> str:
    d = datetime.fromtimestamp((key_ts + DHAKA_OFFSET_MS) // 1000, tz=timezone.utc)
    return f"{d.month:02d}-{d.day:02d}"


def _nearest_rank(sorted_vals: list[int], p: int) -> int:
    if not sorted_vals:
        return 0
    idx = max(0, math.ceil((p / 100) * len(sorted_vals)) - 1)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


@router.get("/analytics")
def analytics(request: Request, db: Session = Depends(get_db)):
    sp = request.query_params

    hours_raw = _num_or_none(sp.get("hours"))
    hours = (
        min(int(math.floor(hours_raw)), 8760)
        if hours_raw is not None and math.isfinite(hours_raw) and hours_raw >= 1
        else 24
    )

    gb_raw = sp.get("group_by")
    group_by = gb_raw if gb_raw in ("hour", "day") else ("day" if hours > 48 else "hour")

    now = now_ms()
    from_ms = now - hours * 3600_000

    logs = db.execute(
        select(
            RequestLog.ts, RequestLog.model, RequestLog.providerName, RequestLog.clientName,
            RequestLog.status, RequestLog.latencyMs, RequestLog.tokensIn, RequestLog.tokensOut,
            RequestLog.via, RequestLog.spoofed, RequestLog.ttftMs,
        ).where(RequestLog.ts >= ms_to_dt(from_ms)).limit(20000)
    ).all()

    # ---- Zero-filled timeseries buckets (ascending) ----
    step = 3600_000 if group_by == "hour" else 86400_000
    key_of = _hour_key if group_by == "hour" else _day_key
    label_of = _hour_label if group_by == "hour" else _day_label

    buckets: dict[int, dict] = {}
    k = key_of(from_ms)
    end = key_of(now)
    while k <= end:
        buckets[k] = {"reqs": 0, "tokens": 0, "errors": 0}
        k += step
    for (ts, _model, _prov, _client, status, _lat, t_in, t_out, _via, _spoof, *_rest) in logs:
        b = buckets.get(key_of(ts_ms(ts)))
        if b is None:
            continue
        b["reqs"] += 1
        b["tokens"] += t_in + t_out
        if status >= 400:
            b["errors"] += 1
    timeseries = [
        {"bucket": label_of(key), "reqs": b["reqs"], "tokens": b["tokens"], "errors": b["errors"]}
        for key, b in sorted(buckets.items())
    ]

    # ---- Top groupings ----
    model_agg: dict[str, dict] = {}
    provider_agg: dict[str, int] = {}
    client_agg: dict[str, int] = {}
    for (_ts, model, prov, client, _status, _lat, t_in, t_out, _via, _spoof, *_rest) in logs:
        m = model_agg.setdefault(model, {"reqs": 0, "tokens": 0})
        m["reqs"] += 1
        m["tokens"] += t_in + t_out
        provider_agg[prov] = provider_agg.get(prov, 0) + 1
        client_agg[client] = client_agg.get(client, 0) + 1

    top_models = [
        {"model": model, "reqs": v["reqs"], "tokens": v["tokens"]}
        for model, v in sorted(model_agg.items(), key=lambda kv: (-kv[1]["reqs"], -kv[1]["tokens"]))[:8]
    ]
    top_providers = [
        {"provider": p, "reqs": c}
        for p, c in sorted(provider_agg.items(), key=lambda kv: -kv[1])[:8]
    ]
    top_clients = [
        {"client": c, "reqs": n}
        for c, n in sorted(client_agg.items(), key=lambda kv: -kv[1])[:8]
    ]

    # ---- Summary ----
    total_tokens = sum(row[6] + row[7] for row in logs)
    tokens_in = sum(row[6] for row in logs)
    tokens_out = sum(row[7] for row in logs)
    error_count = sum(1 for row in logs if row[4] >= 400)
    latencies = sorted(row[5] for row in logs)
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    # TTFT (time to first token) is recorded by the streaming paths only; rows
    # without an observed first token are 0 and excluded from the percentiles.
    ttfts = sorted(int(row[10] or 0) for row in logs if int(row[10] or 0) > 0)
    cache_hits = sum(1 for row in logs if row[8] in ("cache", "cache-hit"))

    return {
        "hours": hours,
        "group_by": group_by,
        "timeseries": timeseries,
        "top_models": top_models,
        "top_providers": top_providers,
        "top_clients": top_clients,
        "summary": {
            "total_requests": len(logs),
            "total_tokens": total_tokens,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "avg_latency_ms": r1(avg_latency),
            "p50_latency_ms": _nearest_rank(latencies, 50),
            "p90_latency_ms": _nearest_rank(latencies, 90),
            "p95_latency_ms": _nearest_rank(latencies, 95),
            "p99_latency_ms": _nearest_rank(latencies, 99),
            "ttft_p50_ms": _nearest_rank(ttfts, 50),
            "ttft_p95_ms": _nearest_rank(ttfts, 95),
            "ttft_samples": len(ttfts),
            "error_count": error_count,
            "error_rate": r1((error_count / len(logs)) * 100) if logs else 0,
            "spoofed_fallbacks_count": sum(1 for row in logs if row[9]),
            "cache_hits": cache_hits,
            "cache_hit_rate": r1((cache_hits / len(logs)) * 100) if logs else 0,
            "cache_hits_recent": cache_hits,  # UI alias: same window, same number
        },
    }


# --------------------------------------------------------------------------- #
# GET /logs — most recent request logs, descending by ts
# --------------------------------------------------------------------------- #


@router.get("/logs")
def logs(request: Request, db: Session = Depends(get_db)):
    limit_raw = _num_or_none(request.query_params.get("limit"))
    limit = (
        min(int(math.floor(limit_raw)), 1000)
        if limit_raw is not None and math.isfinite(limit_raw) and limit_raw >= 1
        else 100
    )

    rows = db.scalars(select(RequestLog).order_by(RequestLog.ts.desc()).limit(limit)).all()

    return [
        {
            "id": l.id,
            "ts": ts_ms(l.ts),
            "client_name": l.clientName,
            "provider_name": l.providerName,
            "model": l.model,
            "upstream_model": l.upstreamModel,
            "endpoint": l.endpoint,
            "status": l.status,
            "latency_ms": l.latencyMs,
            "tokens_in": l.tokensIn,
            "tokens_out": l.tokensOut,
            "error": l.error,
            "via": l.via,
            "spoofed": l.spoofed,
            "client_key_id": l.clientKeyId,
            "ttft_ms": l.ttftMs or 0,
        }
        for l in rows
    ]
