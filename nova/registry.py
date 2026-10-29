"""Provider-registry integrity helpers.

This module is the single place that knows what a *valid* entry in
`nova/data/provider_registry.json` looks like. `nova.catalog` stays the runtime
reader (it only ever needs to answer "does this provider/model exist and what
are its capabilities"); this module answers the operator question "is the
catalogue itself sane?" and derives the aggregates the free engine needs.

Everything here is read-only, database-free and network-free, so it is safe to
call from tests, from a startup check, or from an admin endpoint.

Field contract for one provider:

    key, name, kind, format, base_url, auth_type, requires_key, color,
    priority, models[]                       required
    models_url, auth_header, passthrough, free_tier   optional

and for one model:

    id, name, ctx, max_out, caps{tools, vision, reasoning}   required

Two invariants are worth spelling out because getting them wrong silently
breaks routing rather than failing loudly:

  * `base_url` is the API **root**, never a full endpoint. The gateway appends
    `/chat/completions` (or `/v1/messages` for Anthropic-format providers) and
    discovery appends `/models`. A stored `/chat/completions` therefore
    requests `/chat/completions/chat/completions` and the provider can never
    answer. `validate()` rejects that.
  * `requires_key: false` *is* the keyless flag. There is deliberately no
    separate `keyless` field, so there is exactly one source of truth for
    "this provider works without an API key".
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable
from urllib.parse import urlsplit

from . import catalog

__all__ = [
    "REQUIRED_PROVIDER_KEYS",
    "REQUIRED_MODEL_KEYS",
    "REQUIRED_CAPS",
    "KNOWN_KINDS",
    "ENDPOINT_SUFFIXES",
    "validate",
    "summary",
    "is_keyless",
    "registry_models_for_engine",
]

# Fields every provider entry must carry.
REQUIRED_PROVIDER_KEYS: tuple[str, ...] = (
    "key", "name", "kind", "format", "base_url", "auth_type",
    "requires_key", "color", "priority", "models",
)

# Fields every model entry must carry.
REQUIRED_MODEL_KEYS: tuple[str, ...] = ("id", "name", "ctx", "max_out", "caps")
REQUIRED_CAPS: tuple[str, ...] = ("tools", "vision", "reasoning")

# Kinds `nova.bootstrap` / `nova.discovery` / `routers.gateway` know how to talk
# to. Anything else would seed a provider row nothing can dispatch.
KNOWN_KINDS = frozenset({"openai", "anthropic", "gemini", "builtin", "exchange"})

# A base_url whose path ends in one of these is an endpoint, not a root.
ENDPOINT_SUFFIXES = frozenset(
    {"models", "chat/completions", "completions", "messages", "embeddings"}
)

_ENGINE_SUFFIX = ":free"


def _providers() -> list[dict[str, Any]]:
    """Catalogue providers, tolerating a malformed top-level document."""
    raw = catalog.providers()
    return [p for p in raw if isinstance(p, dict)]


def _is_http_url(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip() or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _check_base_url(where: str, key: str, url: Any) -> list[str]:
    if not _is_http_url(url):
        return [f"{where}: base_url is not an http(s) URL"]
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        return [f"{where}: base_url must not carry a query string or fragment"]
    if "{account_id}" not in parts.path and parts.path.rstrip("/").endswith("/models"):
        return [f"{where}: base_url must be the API root, not the /models listing"]
    if parts.path.rstrip("/").endswith("/chat/completions"):
        return [f"{where}: base_url must be the API root, not /chat/completions "
                f"(the gateway appends that path itself)"]
    if parts.path.rstrip("/").endswith(("/messages", "/completions", "/embeddings")):
        return [f"{where}: base_url must be the API root, not a specific endpoint"]
    if key == "openai" and not parts.path.rstrip("/").endswith("/v1"):
        return [f"{where}: an OpenAI-format root should end in /v1"]
    return []


def _check_model(where: str, model: Any, seen: set[str]) -> list[str]:
    problems: list[str] = []
    if not isinstance(model, dict):
        return [f"{where}: model entry is not an object"]
    for field in REQUIRED_MODEL_KEYS:
        if field not in model:
            problems.append(f"{where}: missing model field '{field}'")
    mid = model.get("id")
    if not isinstance(mid, str) or not mid.strip():
        problems.append(f"{where}: model id is empty or not a string")
    elif mid in seen:
        problems.append(f"{where}: duplicate model id '{mid}'")
    else:
        seen.add(mid)
    for field in ("ctx", "max_out"):
        value = model.get(field)
        if field in model and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            problems.append(f"{where}: '{field}' must be a non-negative integer")
    caps = model.get("caps")
    if "caps" in model:
        if not isinstance(caps, dict):
            problems.append(f"{where}: 'caps' must be an object")
        else:
            for cap in REQUIRED_CAPS:
                if cap not in caps:
                    problems.append(f"{where}: caps is missing '{cap}'")
                elif not isinstance(caps[cap], bool):
                    problems.append(f"{where}: caps.{cap} must be a boolean")
    return problems


def validate() -> list[str]:
    """Return every problem with the registry; an empty list means it is sane.

    Checks the schema each consumer relies on, cross-entry constraints
    (duplicate provider key, duplicate model id within a provider), and that
    each keyless provider can actually be dialled without a credential.
    Messages are human-readable and prefixed with the offending provider key.
    """
    problems: list[str] = []
    seen_provider_keys: set[str] = set()

    for index, provider in enumerate(catalog.providers()):
        if not isinstance(provider, dict):
            problems.append(f"provider #{index}: entry is not an object")
            continue
        raw_key = provider.get("key")
        where = f"provider '{raw_key}'" if isinstance(raw_key, str) and raw_key else f"provider #{index}"

        for field in REQUIRED_PROVIDER_KEYS:
            if field not in provider:
                problems.append(f"{where}: missing field '{field}'")

        if isinstance(raw_key, str) and raw_key:
            folded = raw_key.lower()
            if folded in seen_provider_keys:
                problems.append(f"{where}: duplicate provider key")
            seen_provider_keys.add(folded)

        kind = provider.get("kind")
        if kind not in KNOWN_KINDS:
            problems.append(f"{where}: unknown kind '{kind}'")

        requires_key = provider.get("requires_key")
        if "requires_key" in provider and not isinstance(requires_key, bool):
            problems.append(f"{where}: 'requires_key' must be a boolean")

        auth_type = provider.get("auth_type")
        if requires_key is False and auth_type not in ("none", None):
            problems.append(
                f"{where}: requires_key is false but auth_type is '{auth_type}' "
                f"(a keyless provider cannot require credentials)"
            )

        if "base_url" in provider:
            problems.extend(_check_base_url(where, str(raw_key or ""), provider.get("base_url")))

        free_tier = provider.get("free_tier")
        if free_tier is not None and not isinstance(free_tier, str):
            problems.append(f"{where}: 'free_tier' must be a string or null")

        models = provider.get("models")
        if not isinstance(models, list):
            problems.append(f"{where}: 'models' must be a list")
            continue
        if not models:
            problems.append(f"{where}: declares no models")
            continue
        seen_models: set[str] = set()
        for model in models:
            problems.extend(_check_model(where, model, seen_models))

    return problems


def _is_free_model(model: dict[str, Any]) -> bool:
    """A model is free when advertised free or when vendor + pricing say so.

    Mirrors the gateway's own definition (`isFree` next to a free model): a
    `:free` id, a vendor's `-Free` spelling, or a keyless provider — where no
    credential exists to bill against.
    """
    mid = str(model.get("id") or "")
    return (
        mid.endswith(_ENGINE_SUFFIX)
        or mid.endswith("-Free")
        or bool(model.get("free"))
    )


def summary() -> dict[str, Any]:
    """Aggregate counts over the registry.

    `providers` and `models` deliberately match `nova.catalog.source()` so the
    dashboard's two numbers can never disagree; the rest describes the free
    surface (`keyless_providers`, `free_models`) and the wire-format mix.
    """
    providers = _providers()
    model_count = 0
    keyless = 0
    free_models = 0
    kinds: Counter[str] = Counter()
    for provider in providers:
        models = provider.get("models") or []
        models = [m for m in models if isinstance(m, dict)]
        model_count += len(models)
        kinds[str(provider.get("kind"))] += 1
        if provider.get("requires_key") is False:
            keyless += 1
            free_models += len(models)
        else:
            free_models += sum(1 for m in models if _is_free_model(m))
    return {
        "providers": len(providers),
        "models": model_count,
        "keyless_providers": keyless,
        "free_models": free_models,
        "kinds": dict(sorted(kinds.items())),
    }


def is_keyless(provider: dict[str, Any]) -> bool:
    """Whether a provider entry can be dialled with no API key."""
    return provider.get("requires_key") is False


def _engine_models() -> Iterable[dict[str, Any]]:
    """Dedupe key: `:free` variants collapse onto their base id."""
    for provider in _providers():
        if not is_keyless(provider):
            continue
        pkey = str(provider.get("key") or "")
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            mid = str(model.get("id") or "")
            if not mid:
                continue
            yield {
                "id": mid[: -len(_ENGINE_SUFFIX)] if mid.endswith(_ENGINE_SUFFIX) else mid,
                "provider": pkey,
                "ctx": int(model.get("ctx") or 0),
                "original_id": mid,
            }


def registry_models_for_engine() -> list[dict[str, Any]]:
    """Every model the free engine can offer from the keyless providers.

    Keyless providers need no credential, so their whole model list is
    reachable for free. `:free`-suffixed ids are advertised back by the engine
    with the suffix removed (the suffix is a vendor billing marker, not part
    of the upstream model name), and the result is de-duplicated by that
    normalised id — the first provider wins, so the order is the registry order.

    Returns `[{"id", "provider", "ctx", "original_id"}, ...]`.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in _engine_models():
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out