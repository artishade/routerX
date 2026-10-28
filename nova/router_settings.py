"""Router settings — the persisted half of `nova.routing`.

`nova/routing.py` is deliberately database-free; this is the thin layer that
binds it to `SystemConfig` (through `nova.kv`) so the dashboard can read and
write the router's configuration the way RouterX persists its settings:

  routing_router      — strategy + scoring weights + lockout tuning
  routing_policies    — access / routing / budget policies
  routing_fallbacks   — per-model fallback chains
  routing_model_exposure — allow/deny globs
  routing_lockouts    — lockout state (written by the gateway, not the UI)

Every key is additive: a missing key means "defaults", so an existing
deployment keeps routing exactly as it did until an operator changes something.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from . import routing
from .kv import get_config_json, set_config_json

DEFAULT_STRATEGY = "priority"


@dataclass
class RouterSettings:
    """Everything the router needs for one dispatch, loaded once per request."""

    strategy: str = DEFAULT_STRATEGY
    weights: dict[str, float] = field(default_factory=lambda: dict(routing.DEFAULT_WEIGHTS))
    lockout_threshold: int = routing.DEFAULT_LOCKOUT_THRESHOLD
    lockout_base_ms: int = routing.DEFAULT_LOCKOUT_MS
    policies: list[dict[str, Any]] = field(default_factory=list)
    chains: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    denylist: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    last_known_good: str = ""
    sla: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "weights": dict(self.weights),
            "lockout_threshold": self.lockout_threshold,
            "lockout_base_ms": self.lockout_base_ms,
            "last_known_good": self.last_known_good,
            "sla": dict(self.sla),
        }


def load(db: Session) -> RouterSettings:
    """Read every routing key. A malformed value falls back to its default."""
    router = get_config_json(db, routing.CFG_ROUTER, {}) or {}
    if not isinstance(router, dict):
        router = {}
    strategy = str(router.get("strategy", DEFAULT_STRATEGY)).strip().lower()
    if strategy not in routing.STRATEGIES:
        strategy = DEFAULT_STRATEGY

    weights = routing.normalize_weights(
        router.get("weights") if isinstance(router.get("weights"), dict) else None
    )
    try:
        threshold = max(1, int(router.get("lockout_threshold", routing.DEFAULT_LOCKOUT_THRESHOLD)))
    except (TypeError, ValueError):
        threshold = routing.DEFAULT_LOCKOUT_THRESHOLD
    try:
        base_ms = max(1, int(router.get("lockout_base_ms", routing.DEFAULT_LOCKOUT_MS)))
    except (TypeError, ValueError):
        base_ms = routing.DEFAULT_LOCKOUT_MS

    exposure = get_config_json(db, routing.CFG_EXPOSURE, {}) or {}
    if not isinstance(exposure, dict):
        exposure = {}

    return RouterSettings(
        strategy=strategy,
        weights=weights,
        lockout_threshold=threshold,
        lockout_base_ms=base_ms,
        policies=routing.load_policies(get_config_json(db, routing.CFG_POLICIES, [])),
        chains=routing.load_chains(get_config_json(db, routing.CFG_FALLBACKS, {})),
        denylist=[str(x) for x in (exposure.get("denylist") or [])],
        allowlist=[str(x) for x in (exposure.get("allowlist") or [])],
        last_known_good=str(router.get("last_known_good", "") or ""),
        sla=router.get("sla") if isinstance(router.get("sla"), dict) else {},
    )


def save(db: Session, **fields: Any) -> dict[str, Any]:
    """Persist the router knobs. Unknown keys are ignored, not written."""
    router = get_config_json(db, routing.CFG_ROUTER, {}) or {}
    if not isinstance(router, dict):
        router = {}

    if "strategy" in fields and fields["strategy"]:
        value = str(fields["strategy"]).strip().lower()
        if value in routing.STRATEGIES:
            router["strategy"] = value
    if isinstance(fields.get("weights"), dict):
        router["weights"] = routing.normalize_weights(fields["weights"])
    for key in ("lockout_threshold", "lockout_base_ms"):
        if key in fields and fields[key] is not None:
            try:
                router[key] = max(1, int(fields[key]))
            except (TypeError, ValueError):
                pass
    if "last_known_good" in fields and fields["last_known_good"] is not None:
        router["last_known_good"] = str(fields["last_known_good"]).strip()
    if isinstance(fields.get("sla"), dict):
        router["sla"] = fields["sla"]

    set_config_json(db, routing.CFG_ROUTER, router)

    if isinstance(fields.get("policies"), list):
        set_config_json(db, routing.CFG_POLICIES, routing.load_policies(fields["policies"]))
    if isinstance(fields.get("chains"), dict):
        set_config_json(db, routing.CFG_FALLBACKS, routing.load_chains(fields["chains"]))

    exposure = get_config_json(db, routing.CFG_EXPOSURE, {}) or {}
    if not isinstance(exposure, dict):
        exposure = {}
    for key in ("denylist", "allowlist"):
        if isinstance(fields.get(key), list):
            exposure[key] = [str(x) for x in fields[key]]
    set_config_json(db, routing.CFG_EXPOSURE, exposure)

    return load(db).to_dict()


def save_lockouts(db: Session, registry: routing.LockoutRegistry) -> None:
    """Persist the live lockout set (best-effort — routing never blocks on it)."""
    try:
        set_config_json(db, routing.CFG_LOCKOUTS, registry.dump_state())
    except Exception:
        pass


def load_lockouts(db: Session, registry: routing.LockoutRegistry) -> None:
    try:
        registry.load_state(get_config_json(db, routing.CFG_LOCKOUTS, []))
    except Exception:
        pass


__all__ = ["RouterSettings", "DEFAULT_STRATEGY", "load", "save", "save_lockouts", "load_lockouts"]
