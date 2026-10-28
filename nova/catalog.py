"""Provider + model catalogue — the RouterX curated registry.

`nova/data/provider_registry.json` is RouterX's own curated, hand-maintained
set of providers: every one speaks an OpenAI / Anthropic / Gemini wire format,
is keyless or has a usable free tier, and is reachable in practice. Dead and
unofficial proxy entries are deliberately kept out. Each provider carries its
active models and the capabilities RouterX declares for them.

This module is read-only and database-free. It backs three things:

  • `/v1/models` and the admin catalogue endpoints (what exists at all),
  • the exposure allow/deny lists,
  • the per-model capability answer the router needs before it dispatches.

Nothing here talks to a network or a Session; `nova.bootstrap` materializes the
catalogue into `Provider`/`Model` rows additively, `nova.routing` decides which
of those rows a request actually uses, and `nova.registry` validates and
summarises the file itself.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

DATA_FILE = Path(__file__).resolve().parent / "data" / "provider_registry.json"

# Formats the catalogue marks as accepting any model id, passthrough style. RouterX
# never dials these blind — they stay in the catalogue, but a request still has
# to name a model we know about.
_MODEL_EXPOSURE_MAX_ENTRIES = 500
_MODEL_EXPOSURE_MAX_LENGTH = 200


# --------------------------------------------------------------------------- #
# Glob matching (port of src/shared/utils/globPattern.ts)
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=2048)
def _compiled_glob(pattern: str) -> re.Pattern[str]:
    """Translate a `*`/`?` glob into an anchored regex (cached)."""
    out = ["^"]
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return re.compile("".join(out))


def glob_match(pattern: str, value: str) -> bool:
    """Case-insensitive glob match. `*` spans `/`, `?` is a single character."""
    if not pattern:
        return False
    return bool(_compiled_glob(pattern.lower()).match(value.lower()))


# --------------------------------------------------------------------------- #
# Catalogue load
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _catalogue() -> dict[str, Any]:
    try:
        with DATA_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # a missing/corrupt catalogue must never break the gateway
        return {"providers": [], "models_by_id": {}, "source": "", "generated_at": ""}
    if not isinstance(data, dict):
        return {"providers": [], "models_by_id": {}, "source": "", "generated_at": ""}
    data.setdefault("providers", [])
    index: dict[str, list[dict[str, Any]]] = {}
    for provider in data["providers"]:
        for model in provider.get("models", []):
            index.setdefault(model["id"], []).append(model)
    data["models_by_id"] = index
    return data


def source() -> dict[str, Any]:
    """Provenance of the catalogue (surfaced in the dashboard + /v1/models)."""
    cat = _catalogue()
    return {
        "source": cat.get("source", ""),
        "generated_at": cat.get("generated_at", ""),
        "lifecycle_snapshot": cat.get("lifecycle_snapshot", ""),
        "providers": len(cat.get("providers", [])),
        "models": sum(len(p.get("models", [])) for p in cat.get("providers", [])),
    }


def providers() -> list[dict[str, Any]]:
    return _catalogue()["providers"]


def provider(key: str) -> dict[str, Any] | None:
    """Look a provider up by its catalogue key."""
    needle = (key or "").strip().lower()
    if not needle:
        return None
    for p in providers():
        if p["key"].lower() == needle:
            return p
    return None


def provider_models(key: str) -> list[dict[str, Any]]:
    p = provider(key)
    return list(p["models"]) if p else []


def model(entry_provider: str, model_id: str) -> dict[str, Any] | None:
    for m in provider_models(entry_provider):
        if m["id"] == model_id:
            return m
    return None


def providers_for_model(model_id: str) -> list[str]:
    """Every provider key that serves `model_id` (bare id, not alias-expanded)."""
    cat = _catalogue()
    out: list[str] = []
    for p in cat["providers"]:
        for m in p["models"]:
            if m["id"] == model_id:
                out.append(p["key"])
                break
    return out


def _richness(m: dict[str, Any]) -> int:
    """How much a catalogue entry declares — used to break cross-provider ties.

    Port of `modelRichness()` in `open-sse/config/providerModels.ts`: when the
    same model id is declared by several providers, the entry that carries the
    most capability metadata wins, because it is the one we can reason about.
    """
    score = 0
    if m.get("efforts"):
        score += 10
    if m.get("caps", {}).get("reasoning"):
        score += 5
    if m.get("ctx"):
        score += 3
    if m.get("max_out"):
        score += 2
    if m.get("caps", {}).get("vision"):
        score += 2
    if m.get("unsupported"):
        score += 1
    return score


def resolve(model_id: str) -> dict[str, Any] | None:
    """Resolve a bare or `vendor/model` id to its best catalogue entry.

    Three steps, in order (port of `getGlobalModel()`): exact id, then the
    basename with the `vendor/` prefix stripped, then a longest-prefix match for
    suffixed spellings like `kimi-k3-free` → `kimi-k3`.
    """
    model_id = (model_id or "").strip()
    if not model_id:
        return None
    cat = _catalogue()
    index = cat["models_by_id"]

    exact = index.get(model_id)
    if exact:
        return max(exact, key=_richness)

    basename = model_id.rsplit("/", 1)[-1]
    by_base = index.get(basename)
    if by_base:
        return max(by_base, key=_richness)

    best: dict[str, Any] | None = None
    for entries in index.values():
        for m in entries:
            if basename.startswith(m["id"]) and (
                best is None
                or len(m["id"]) > len(best["id"])
                or (len(m["id"]) == len(best["id"]) and _richness(m) > _richness(best))
            ):
                best = m
    return best


def is_known(model_id: str) -> bool:
    return resolve(model_id) is not None


def capabilities(model_id: str) -> dict[str, bool]:
    """Capabilities for a model, defaulting every flag to False."""
    entry = resolve(model_id)
    caps = (entry or {}).get("caps") or {}
    return {
        "tools": bool(caps.get("tools")),
        "vision": bool(caps.get("vision")),
        "reasoning": bool(caps.get("reasoning")),
        "audio": bool(caps.get("audio")),
        "video": bool(caps.get("video")),
    }


def context_length(model_id: str) -> int:
    return int((resolve(model_id) or {}).get("ctx") or 0)


def max_output(model_id: str) -> int:
    return int((resolve(model_id) or {}).get("max_out") or 0)


def unsupported_params(model_id: str) -> list[str]:
    return list((resolve(model_id) or {}).get("unsupported") or [])


def thinking_efforts(model_id: str) -> list[str]:
    return list((resolve(model_id) or {}).get("efforts") or [])


# --------------------------------------------------------------------------- #
# Exposure allow/deny lists (port of src/shared/utils/modelExposureList.ts)
# --------------------------------------------------------------------------- #

def _clean_entries(entries: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for raw in entries or ():
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if value and len(value) <= _MODEL_EXPOSURE_MAX_LENGTH:
            out.append(value)
        if len(out) >= _MODEL_EXPOSURE_MAX_ENTRIES:
            break
    return out


def is_exposure_allowed(model_id: str, provider_key: str = "", *,
                        denylist: Iterable[str] | None = None,
                        allowlist: Iterable[str] | None = None) -> bool:
    """Whether `model_id` may be advertised / enter a candidate pool.

    Two independent opt-in lists, deny first. An entry is a bare id
    (`gpt-4o`), a provider-prefixed id (`openai/gpt-4o`) or a glob
    (`anthropic/*`). Both empty means "expose everything" — the same default
    the catalogue ships, so an untouched deployment keeps its current catalogue.
    """
    ids = [i for i in (model_id, f"{provider_key}/{model_id}" if provider_key else "") if i]
    # A bare pattern ("gpt-*") must also match a prefixed id ("openai/gpt-4o"),
    # which is the form every seeded model is exposed under.
    ids += [i.rsplit("/", 1)[-1] for i in list(ids) if "/" in i]
    denied = _clean_entries(denylist)
    allowed = _clean_entries(allowlist)
    if not denied and not allowed:
        return True
    for pattern in denied:
        if any(glob_match(pattern, i) for i in ids):
            return False
    if allowed:
        return any(glob_match(p, i) for p in allowed for i in ids)
    return True


def search(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Substring search over model ids/names and provider keys."""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: list[dict[str, Any]] = []
    for p in providers():
        for m in p["models"]:
            hay = f"{m['id']}\n{m.get('name', '')}\n{p['key']}\n{p['name']}".lower()
            if q in hay:
                hits.append({
                    "provider": p["key"],
                    "provider_name": p["name"],
                    "kind": p["kind"],
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "context_length": m.get("ctx", 0),
                    "max_output": m.get("max_out", 0),
                    "capabilities": m.get("caps", {}),
                })
    return hits[: max(1, limit)]


__all__ = [
    "DATA_FILE", "glob_match", "source", "providers", "provider", "provider_models",
    "model", "providers_for_model", "resolve", "is_known", "capabilities",
    "context_length", "max_output", "unsupported_params", "thinking_efforts",
    "is_exposure_allowed", "search",
]
