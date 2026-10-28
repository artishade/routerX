"""Routing + catalogue admin routes, mounted under /api/admin.

Everything here is the operator-facing surface of the routing brain:

  GET  /routing                    current router settings + live availability
  POST /routing                    save strategy / weights / lockout tuning /
                                   policies / fallback chains / exposure lists
  GET  /routing/availability       per-(provider, model) lockouts, cooling-down
  POST /routing/availability/clear clear one lockout, or all of them
  GET/POST /routing/profiles       named `auto:<name>` chains (list / upsert)
  DELETE /routing/profiles/{name}  remove a named chain
  GET  /catalog                    the curated provider registry, searchable
  GET  /catalog/{key}              one provider and its full model list
  GET  /skills                     the curated agent-skill catalogue
  GET  /mcp/scopes                 MCP scope table + tool → scope mapping

Reads are cheap and never mutate. Writes go through `nova.router_settings`,
which is the only thing that writes routing config, so a bad payload can only
be ignored — never half-applied.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from nova import catalog as nova_catalog
from nova import mcp_tools, profiles as nova_profiles, router_settings
from nova import routing as nova_routing
from nova.database import get_db

router = APIRouter()


async def _body(request: Request) -> dict:
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _router_payload(db: Session) -> dict:
    settings = router_settings.load(db)
    return {
        "settings": settings.to_dict(),
        "strategies": list(nova_routing.STRATEGIES),
        "default_weights": dict(nova_routing.DEFAULT_WEIGHTS),
        "policies": settings.policies,
        "chains": settings.chains,
        "exposure": {"denylist": settings.denylist, "allowlist": settings.allowlist},
        "availability": nova_routing.LOCKOUTS.report(),
        "profiles": nova_profiles.load_profiles(db),
    }


# --------------------------------------------------------------------------- #
# Router configuration
# --------------------------------------------------------------------------- #


@router.get("/routing")
def get_routing(db: Session = Depends(get_db)):
    return JSONResponse(_router_payload(db))


@router.post("/routing")
async def save_routing(request: Request, db: Session = Depends(get_db)):
    body = await _body(request)
    saved = router_settings.save(
        db,
        strategy=body.get("strategy"),
        weights=body.get("weights") if isinstance(body.get("weights"), dict) else None,
        lockout_threshold=body.get("lockout_threshold"),
        lockout_base_ms=body.get("lockout_base_ms"),
        last_known_good=body.get("last_known_good"),
        sla=body.get("sla") if isinstance(body.get("sla"), dict) else None,
        policies=body.get("policies") if isinstance(body.get("policies"), list) else None,
        chains=body.get("chains") if isinstance(body.get("chains"), dict) else None,
        denylist=body.get("denylist") if isinstance(body.get("denylist"), list) else None,
        allowlist=body.get("allowlist") if isinstance(body.get("allowlist"), list) else None,
    )
    return JSONResponse({"settings": saved, "availability": nova_routing.LOCKOUTS.report()})


@router.get("/routing/availability")
def availability():
    return JSONResponse({
        "lockouts": nova_routing.LOCKOUTS.report(),
        "threshold": nova_routing.LOCKOUTS.threshold,
        "base_ms": nova_routing.LOCKOUTS.base_ms,
    })


@router.post("/routing/availability/clear")
async def clear_availability(request: Request, db: Session = Depends(get_db)):
    body = await _body(request)
    provider = str(body.get("provider", "") or "")
    model = str(body.get("model", "") or "")
    if provider and model:
        cleared = nova_routing.LOCKOUTS.clear(provider, model)
    else:
        nova_routing.LOCKOUTS.reset()
        cleared = True
    router_settings.save_lockouts(db, nova_routing.LOCKOUTS)
    return JSONResponse({"cleared": cleared, "lockouts": nova_routing.LOCKOUTS.report()})


# --------------------------------------------------------------------------- #
# Named routing profiles (`auto:<name>`)
# --------------------------------------------------------------------------- #

def _profile_name(raw) -> str:
    """Profile names are URL/model-id safe: lower-case, no spaces or colons."""
    name = str(raw or "").strip().lower()
    if name.startswith(nova_profiles.AUTO_PREFIX):
        name = name[len(nova_profiles.AUTO_PREFIX):]
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "-_.")
    return cleaned[:40]


@router.get("/routing/profiles")
def list_profiles(db: Session = Depends(get_db)):
    """Every profile plus the strategies a profile may use."""
    stored = nova_profiles.load_profiles(db)
    return JSONResponse({
        "profiles": [{"id": f"auto:{name}", "name": name, **spec} for name, spec in stored.items()],
        "strategies": list(nova_routing.STRATEGIES),
        "auto_standins_default": True,
    })


@router.post("/routing/profiles")
async def save_profile(request: Request, db: Session = Depends(get_db)):
    """Create or update one profile: `{name, fallbacks, strategy, auto_standins, note}`."""
    body = await _body(request)
    name = _profile_name(body.get("name") or body.get("id"))
    if not name:
        return JSONResponse({"error": "Profile name is required (letters, digits, - _ .)"},
                            status_code=400)
    fallbacks = body.get("fallbacks")
    if fallbacks is not None and not isinstance(fallbacks, (list, str)):
        return JSONResponse({"error": "fallbacks must be a list of model ids"}, status_code=400)
    strategy = body.get("strategy")
    if strategy not in (None, "") and str(strategy).strip().lower() not in nova_routing.STRATEGIES:
        return JSONResponse({"error": f"Unknown strategy '{strategy}'",
                             "strategies": list(nova_routing.STRATEGIES)}, status_code=400)

    stored = nova_profiles.load_profiles(db)
    existing = stored.get(name, {})
    spec = {
        "fallbacks": fallbacks if fallbacks is not None else existing.get("fallbacks", []),
        "strategy": strategy if strategy not in (None, "") else existing.get("strategy", ""),
        "auto_standins": body.get("auto_standins", existing.get("auto_standins", True)),
        "note": body.get("note", existing.get("note", "")),
    }
    stored[name] = nova_profiles.normalize_spec(spec)
    nova_profiles.save_profiles(db, stored)
    return JSONResponse({"ok": True, "profile": {"id": f"auto:{name}", "name": name, **stored[name]},
                         "profiles": nova_profiles.load_profiles(db)})


@router.delete("/routing/profiles/{name}")
def delete_profile(name: str, db: Session = Depends(get_db)):
    key = _profile_name(name)
    stored = nova_profiles.load_profiles(db)
    if key not in stored:
        return JSONResponse({"error": f"Unknown profile '{key}'"}, status_code=404)
    stored.pop(key)
    nova_profiles.save_profiles(db, stored)
    return JSONResponse({"ok": True, "removed": key, "profiles": nova_profiles.load_profiles(db)})


# --------------------------------------------------------------------------- #
# Curated catalogue
# --------------------------------------------------------------------------- #


@router.get("/catalog")
def catalog(provider: str = "", q: str = "", limit: int = 200):
    """Registry summary, or a search across provider keys, names and models."""
    if q:
        return JSONResponse({"results": nova_catalog.search(q, limit=max(1, min(limit, 500)))})

    providers_out = []
    for p in nova_catalog.providers():
        if provider and p["key"] != provider:
            continue
        providers_out.append({
            "key": p["key"],
            "name": p["name"],
            "kind": p["kind"],
            "format": p["format"],
            "base_url": p["base_url"],
            "requires_key": p.get("requires_key", True),
            "passthrough": p.get("passthrough", False),
            "models": len(p["models"]),
        })
    return JSONResponse({
        "source": nova_catalog.source(),
        "total": len(providers_out),
        "providers": providers_out[: max(1, min(limit, 1000))],
    })


@router.get("/catalog/{key}")
def catalog_provider(key: str):
    provider = nova_catalog.provider(key)
    if provider is None:
        return JSONResponse({"error": f"Unknown provider '{key}'"}, status_code=404)
    return JSONResponse({
        "key": provider["key"],
        "name": provider["name"],
        "kind": provider["kind"],
        "format": provider["format"],
        "base_url": provider["base_url"],
        "auth_type": provider.get("auth_type"),
        "requires_key": provider.get("requires_key", True),
        "models": [
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "context_length": m.get("ctx", 0),
                "max_output": m.get("max_out", 0),
                "capabilities": m.get("caps", {}),
                "exposed_id": f"{provider['key']}/{m['id']}",
            }
            for m in provider["models"]
        ],
    })


# --------------------------------------------------------------------------- #
# Skills + MCP scopes
# --------------------------------------------------------------------------- #


@router.get("/skills")
def skills(q: str = ""):
    if q:
        return JSONResponse({"skills": mcp_tools.search_skills(q)})
    catalogue = mcp_tools.skills()
    return JSONResponse({
        "total": len(catalogue),
        "skills": catalogue,
        "areas": sorted({s["area"] for s in catalogue}),
        "categories": sorted({s["category"] for s in catalogue}),
    })


@router.get("/mcp/scopes")
def mcp_scopes():
    return JSONResponse({
        "scopes": mcp_tools.scopes(),
        "tool_scopes": mcp_tools.tool_scopes(),
    })
