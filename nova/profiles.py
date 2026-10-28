"""Named routing profiles for the `auto:<profile>` virtual model id.

`auto` is the operator's default chain (whatever `ModelRoute` and the router
strategy produce). `auto:fast`, `auto:cheap` and friends name a *profile*: a
fallback list plus an optional strategy that overrides the defaults for that
one request, so a client can ask for "the cheap chain" without a new model id
per upstream.

Profiles live in `SystemConfig` under `routing_profiles`:

    {"fast": {"fallbacks": ["groq/llama-3.3-70b-versatile", "openrouter/x:free"],
              "strategy": "latency",
              "auto_standins": true,
              "note": "low-latency chain"}}

`resolve_profile()` is pure: given the requested id and the stored mapping it
returns `(name, spec)` - `("", None)` for plain `auto` or a normal model id,
`(name, None)` when the id names a profile that does not exist (so the caller
can answer 404 or fall back), and `(name, normalized_spec)` for a real one.
The response `model` always stays the requested id; the profile only changes
*how* the request is routed.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import routing

CFG_PROFILES = "routing_profiles"
AUTO_PREFIX = "auto:"
STRATEGY_KEYS = tuple(routing.STRATEGIES)

__all__ = [
    "CFG_PROFILES", "AUTO_PREFIX", "parse_profile_id", "normalize_spec",
    "load_profiles", "save_profiles", "resolve_profile", "profile_ids",
]


def parse_profile_id(requested: str) -> str | None:
    """`"auto:fast"` -> `"fast"`; `"auto"` -> `""`; anything else -> `None`."""
    value = (requested or "").strip()
    if value == "auto":
        return ""
    if value.startswith(AUTO_PREFIX):
        return value[len(AUTO_PREFIX):].strip()
    return None


def _fallbacks(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [part for part in raw.replace(";", ",").split(",")]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("id") or item.get("model") or ""
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def normalize_spec(raw: Any) -> dict[str, Any]:
    """One stored profile -> a complete spec (missing fields defaulted)."""
    raw = raw if isinstance(raw, dict) else {}
    strategy = str(raw.get("strategy") or "").strip().lower()
    return {
        "fallbacks": _fallbacks(raw.get("fallbacks")),
        "strategy": strategy if strategy in STRATEGY_KEYS else "",
        "auto_standins": raw.get("auto_standins", True) is not False,
        "note": str(raw.get("note") or "")[:500],
    }


def load_profiles(db: Session) -> dict[str, dict[str, Any]]:
    """Every stored profile, normalized (`{name: spec}`)."""
    from .kv import get_config_json

    raw = get_config_json(db, CFG_PROFILES, {}) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): normalize_spec(spec) for name, spec in raw.items() if str(name).strip()}


def save_profiles(db: Session, profiles: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from .kv import set_config_json

    clean = {str(k).strip(): normalize_spec(v) for k, v in (profiles or {}).items() if str(k).strip()}
    set_config_json(db, CFG_PROFILES, clean)
    return clean


def resolve_profile(requested: str, profiles: dict[str, Any] | None = None) -> tuple[str, dict[str, Any] | None]:
    """`(profile_name, spec_or_None)` for a requested model id.

    * a normal model id  -> `("", None)`
    * `"auto"`           -> `("", None)`  (the default chain)
    * `"auto:known"`     -> `("known", spec)`
    * `"auto:missing"`   -> `("missing", None)`
    """
    name = parse_profile_id(requested)
    if name is None or name == "":
        return "", None
    stored = (profiles or {}).get(name)
    if stored is None:
        return name, None
    return name, normalize_spec(stored)


def profile_ids(profiles: dict[str, Any] | None = None) -> list[str]:
    """`["auto:fast", "auto:cheap", ...]` for `GET /v1/models`."""
    return [f"{AUTO_PREFIX}{name}" for name in sorted(profiles or {})]
