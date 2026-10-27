"""Routing brain — the OmniRoute domain layer, ported to Python.

Everything that decides *which* upstream actually answers a request lives here,
ported from the OmniRoute domain/autoCombo layer:

  • `tagRouter.ts`        — request `metadata.tags` steer which providers are eligible
  • `policyEngine.ts`     — glob-matched routing / access / budget policies
  • `accountFallback.ts`  — per (provider, model) lockouts after repeated failures
  • `fallbackPolicy.ts`   — declarative per-model fallback chains
  • `combo.ts`            — load-balancing strategies over a candidate set
  • `routerStrategy.ts`   — rules / cost / latency / sla / lkgp pickers
  • `modelExposureList.ts`— allow/deny globs applied before a candidate is used

Nothing here performs I/O. Callers pass plain dicts and get plain dicts back, so
the gateway can drive the same decisions from `/v1/chat/completions`, the
dashboard preview, or a test. State that must outlive a process (lockouts,
chains, policies) is persisted through `SystemConfig` by the caller — see
`load_state` / `dump_state`.
"""
from __future__ import annotations

import math
import random
import threading
import time
from typing import Any, Iterable, Sequence

from .catalog import glob_match, is_exposure_allowed

# Config keys (SystemConfig). Additive: absent means "use the defaults".
CFG_POLICIES = "routing_policies"
CFG_FALLBACKS = "routing_fallback_chains"
CFG_LOCKOUTS = "routing_lockouts"
CFG_ROUTER = "routing_router"
CFG_EXPOSURE = "routing_model_exposure"

DEFAULT_LOCKOUT_THRESHOLD = 3
DEFAULT_LOCKOUT_MS = 60_000
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_DURATION_MS = 15 * 60_000
DEFAULT_ATTEMPT_WINDOW_MS = 5 * 60_000

# Provider / model orderings, cheapest first is a policy, not a fact.
STRATEGIES = (
    "priority", "weighted", "round-robin", "random",
    "least-used", "cost-optimized", "rules", "cost", "latency", "sla", "lkgp",
)
COMBO_STRATEGIES = ("priority", "weighted", "round-robin", "random", "least-used", "cost-optimized")
ROUTER_STRATEGIES = ("rules", "cost", "latency", "sla", "lkgp")


# --------------------------------------------------------------------------- #
# Tag routing — port of src/domain/tagRouter.ts
# --------------------------------------------------------------------------- #

def normalize_tags(value: Any) -> list[str]:
    """Lower-cased, de-duplicated, order-preserving tag list.

    Accepts a list or a comma-separated string; anything else is an empty list.
    """
    raw: list[Any]
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = value.split(",")
    else:
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = item.strip().lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def normalize_match_mode(value: Any) -> str:
    return "all" if isinstance(value, str) and value.strip().lower() == "all" else "any"


def matches_tags(connection_tags: Sequence[str], request_tags: Sequence[str], mode: str = "any") -> bool:
    """Untagged requests match everything; untagged providers match nothing.

    This is the load-bearing asymmetry from OmniRoute: a client that never sends
    tags gets the full pool, while a provider carrying no tags is opt-in.
    """
    if not request_tags:
        return True
    if not connection_tags:
        return False
    pool = set(connection_tags)
    if mode == "all":
        return all(t in pool for t in request_tags)
    return any(t in pool for t in request_tags)


def request_tags(body: dict | None) -> tuple[list[str], str]:
    """Pull `metadata.tags` / `metadata.tag_match_mode` off a request body."""
    metadata = body.get("metadata") if isinstance(body, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    return (
        normalize_tags(metadata.get("tags")),
        normalize_match_mode(metadata.get("tag_match_mode", metadata.get("tagMatchMode"))),
    )


# --------------------------------------------------------------------------- #
# Availability / lockouts — port of accountFallback.ts + modelAvailability.ts
# --------------------------------------------------------------------------- #

class LockoutRegistry:
    """Per-(provider, model) backoff after repeated failures.

    `threshold` consecutive failures lock a pair out; each further failure past
    the threshold doubles the window (base → max) so a genuinely broken upstream
    backs off exponentially instead of being retried on every request. A single
    success clears the record, so a recovered provider rejoins immediately.
    """

    def __init__(self, threshold: int = DEFAULT_LOCKOUT_THRESHOLD,
                 base_ms: int = DEFAULT_LOCKOUT_MS, max_ms: int = 15 * 60_000) -> None:
        self.threshold = max(1, int(threshold))
        self.base_ms = max(1, int(base_ms))
        self.max_ms = max(self.base_ms, int(max_ms))
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}

    # -- keys -------------------------------------------------------------- #
    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider or '-'}::{model or '-'}"

    # -- queries ----------------------------------------------------------- #
    def is_locked(self, provider: str, model: str, now_ms: int | None = None) -> bool:
        now = _now_ms() if now_ms is None else int(now_ms)
        with self._lock:
            entry = self._entries.get(self.key(provider, model))
        return entry is not None and int(entry.get("until", 0)) > now

    def remaining_ms(self, provider: str, model: str, now_ms: int | None = None) -> int:
        now = _now_ms() if now_ms is None else int(now_ms)
        with self._lock:
            entry = self._entries.get(self.key(provider, model))
        if entry is None:
            return 0
        return max(0, int(entry.get("until", 0)) - now)

    def report(self, now_ms: int | None = None) -> list[dict[str, Any]]:
        """Every entry that is still cooling down, longest window first."""
        now = _now_ms() if now_ms is None else int(now_ms)
        out: list[dict[str, Any]] = []
        with self._lock:
            entries = list(self._entries.items())
        for k, e in entries:
            until = int(e.get("until", 0))
            if until <= now:
                continue
            provider, _, model = k.partition("::")
            out.append({
                "provider": provider, "model": model,
                "failure_count": int(e.get("failures", 0)),
                "until_ms": until,
                "remaining_ms": until - now,
                "reason": e.get("reason", ""),
            })
        out.sort(key=lambda r: r["remaining_ms"], reverse=True)
        return out

    def active_failures(self) -> dict[str, int]:
        """Failure counts for entries that are still cooling down."""
        return {LockoutRegistry.key(r["provider"], r["model"]): r["failure_count"]
                for r in self.report()}

    # -- mutations --------------------------------------------------------- #
    def record_failure(self, provider: str, model: str, reason: str = "") -> int:
        """Count one failure; return how long the pair is now locked (0 = not yet)."""
        key = self.key(provider, model)
        now = _now_ms()
        with self._lock:
            entry = self._entries.setdefault(key, {"failures": 0, "until": 0, "reason": ""})
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["reason"] = (reason or entry.get("reason", ""))[:200]
            failures = entry["failures"]
            if failures < self.threshold:
                return 0
            window = min(self.base_ms * (2 ** (failures - self.threshold)), self.max_ms)
            entry["until"] = now + window
            return window

    def record_success(self, provider: str, model: str) -> None:
        with self._lock:
            self._entries.pop(self.key(provider, model), None)

    def clear(self, provider: str, model: str) -> bool:
        with self._lock:
            return self._entries.pop(self.key(provider, model), None) is not None

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    # -- persistence (SystemConfig round-trip) ----------------------------- #
    def dump_state(self) -> list[dict[str, Any]]:
        # The provider/model pair lives in the key, not in the value — split it
        # back out here or every restored entry would come back nameless.
        rows = []
        for k, e in self._entries.items():
            provider, _, model = k.partition("::")
            rows.append({
                "provider": provider, "model": model,
                "failures": int(e.get("failures", 0)), "until": int(e.get("until", 0)),
                "reason": e.get("reason", ""),
            })
        return rows

    def load_state(self, rows: Iterable[dict[str, Any]] | None) -> None:
        with self._lock:
            self._entries.clear()
            for row in rows or ():
                if not isinstance(row, dict):
                    continue
                provider = str(row.get("provider", ""))
                model = str(row.get("model", ""))
                if not provider or not model:
                    continue
                self._entries[self.key(provider, model)] = {
                    "failures": int(row.get("failures", 0) or 0),
                    "until": int(row.get("until", 0) or 0),
                    "reason": str(row.get("reason", "") or ""),
                }


# Process-wide registry. The gateway owns one; tests build their own.
LOCKOUTS = LockoutRegistry()


# --------------------------------------------------------------------------- #
# Policy engine — port of src/domain/policyEngine.ts
# --------------------------------------------------------------------------- #

def load_policies(raw: Any) -> list[dict[str, Any]]:
    """Normalize stored policies into the engine's shape, dropping junk rows.

    Accepts both the nested form an operator writes (`conditions.model_pattern`,
    `actions.block_model`) and the flat form this function itself emits, so a
    save→load round-trip is lossless.
    """
    out: list[dict[str, Any]] = []
    for item in raw or ():
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("type", "")).strip().lower()
        if ptype not in ("routing", "access", "budget"):
            continue
        conditions = item.get("conditions") if isinstance(item.get("conditions"), dict) else item
        actions = item.get("actions") if isinstance(item.get("actions"), dict) else item
        out.append({
            "id": str(item.get("id", "")),
            "name": str(item.get("name", "") or item.get("id", "")),
            "type": ptype,
            "enabled": item.get("enabled", True) is not False,
            "priority": int(item.get("priority", 100) or 100),
            "model_pattern": str(conditions.get("model_pattern", "") or ""),
            "prefer_provider": [str(p) for p in (actions.get("prefer_provider") or [])],
            "block_model": [str(p) for p in (actions.get("block_model") or [])],
            "max_tokens": actions.get("max_tokens"),
        })
    return out


def evaluate_policies(policies: Sequence[dict[str, Any]], model: str) -> dict[str, Any]:
    """Policy evaluation — returns the verdict plus what each policy contributed.

    An access policy carrying `block_model` refuses the request outright when
    the requested model matches one of its globs (or its `model_pattern`); the
    verdict then names the policy, so the gateway answers 403 instead of quietly
    serving the request from another provider. Routing and budget policies only
    accumulate (preferred providers, the tightest token cap).
    """
    verdict: dict[str, Any] = {
        "allowed": True,
        "reason": None,
        "preferred_providers": [],
        "applied": [],
        "max_tokens": None,
    }
    for policy in sorted(policies, key=lambda p: p.get("priority", 100)):
        if not policy.get("enabled", True):
            continue
        pattern = policy.get("model_pattern") or ""
        if pattern and not glob_match(pattern, model):
            continue
        kind = policy.get("type")
        if kind == "routing":
            verdict["preferred_providers"].extend(policy.get("prefer_provider") or [])
        elif kind == "access":
            blocks = [b for b in (policy.get("block_model") or []) if b]
            hit = any(glob_match(b, model) for b in blocks) or (bool(pattern) and not blocks)
            if hit:
                verdict["allowed"] = False
                verdict["reason"] = f"Blocked by the access policy '{policy.get('name', '')}'"
                verdict["applied"].append(str(policy.get("name", "")))
                continue
        elif kind == "budget":
            tokens = policy.get("max_tokens")
            if tokens is not None:
                try:
                    value = int(tokens)
                except (TypeError, ValueError):
                    value = None
                if value is not None:
                    verdict["max_tokens"] = value if verdict["max_tokens"] is None else min(verdict["max_tokens"], value)
        verdict["applied"].append(str(policy.get("name", "")))
    verdict["preferred_providers"] = _dedupe(verdict["preferred_providers"])
    return verdict


# --------------------------------------------------------------------------- #
# Fallback chains — port of src/domain/fallbackPolicy.ts
# --------------------------------------------------------------------------- #

def load_chains(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Stored chains: `{"<model>": [{"id": …, "priority": 0, "enabled": true}]}`.

    Also accepts the plain list form `ModelRoute.fallbacks` uses, so both the
    route table and the richer chain table resolve through one function.
    """
    chains: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw, dict):
        for model, entries in raw.items():
            if isinstance(entries, list):
                chains[str(model)] = _normalize_entries(entries)
    return chains


def _normalize_entries(entries: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            out.append({"id": entry, "priority": index, "enabled": True})
        elif isinstance(entry, dict):
            ident = str(entry.get("id") or entry.get("model") or entry.get("exposedId") or "")
            if not ident:
                continue
            try:
                priority = int(entry.get("priority", index))
            except (TypeError, ValueError):
                priority = index
            out.append({
                "id": ident,
                "priority": priority,
                "enabled": entry.get("enabled", True) is not False,
            })
    out.sort(key=lambda e: e["priority"])
    return out


def resolve_chain(chains: dict[str, list[dict[str, Any]]], model: str,
                  exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Enabled entries for `model`, minus the ones already tried.

    Lower `priority` is tried first, exactly as registered.
    """
    entries = chains.get(model) or []
    skip = {str(e) for e in exclude}
    return [e for e in entries if e["enabled"] and e["id"] not in skip]


def next_fallback(chains: dict[str, list[dict[str, Any]]], model: str,
                  exclude: Iterable[str] = ()) -> str | None:
    chain = resolve_chain(chains, model, exclude)
    return chain[0]["id"] if chain else None


def has_fallback(chains: dict[str, list[dict[str, Any]]], model: str) -> bool:
    return any(e["enabled"] for e in chains.get(model, []))


# --------------------------------------------------------------------------- #
# Client lockout — port of src/domain/lockoutPolicy.ts
# --------------------------------------------------------------------------- #

class ClientLockouts:
    """Sliding-window throttle for failed client authentications.

    `max_attempts` failures inside `window_ms` lock the identifier for
    `duration_ms`; a successful auth clears the history.
    """

    def __init__(self, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 duration_ms: int = DEFAULT_LOCKOUT_DURATION_MS,
                 window_ms: int = DEFAULT_ATTEMPT_WINDOW_MS) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.duration_ms = max(1, int(duration_ms))
        self.window_ms = max(1, int(window_ms))
        self._attempts: dict[str, list[int]] = {}
        self._locked_until: dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, identifier: str) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            until = int(self._locked_until.get(identifier, 0))
            self._prune(identifier, now)
            attempts = len(self._attempts.get(identifier, ()))
        if until > now:
            return {"locked": True, "attempts": attempts, "retry_after_ms": until - now}
        return {"locked": False, "attempts": attempts}

    def is_locked(self, identifier: str) -> bool:
        return self.check(identifier)["locked"]

    def record_failure(self, identifier: str) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            self._prune(identifier, now)
            attempts = self._attempts.setdefault(identifier, [])
            attempts.append(now)
            locked = len(attempts) >= self.max_attempts
            if locked:
                self._locked_until[identifier] = now + self.duration_ms
                attempts.clear()
        return {"locked": locked, "attempts": self.max_attempts if locked else len(attempts)}

    def record_success(self, identifier: str) -> None:
        with self._lock:
            self._attempts.pop(identifier, None)
            self._locked_until.pop(identifier, None)

    def reset(self, identifier: str | None = None) -> None:
        with self._lock:
            if identifier is None:
                self._attempts.clear()
                self._locked_until.clear()
            else:
                self._attempts.pop(identifier, None)
                self._locked_until.pop(identifier, None)

    def _prune(self, identifier: str, now: int) -> None:
        cutoff = now - self.window_ms
        attempts = self._attempts.get(identifier)
        if attempts:
            kept = [t for t in attempts if t > cutoff]
            if kept:
                self._attempts[identifier] = kept
            else:
                self._attempts.pop(identifier, None)
        until = self._locked_until.get(identifier)
        if until is not None and until <= now:
            self._locked_until.pop(identifier, None)


# --------------------------------------------------------------------------- #
# Scoring + strategies — port of autoCombo/scoring.ts + routerStrategy.ts
# --------------------------------------------------------------------------- #

# OmniRoute's DEFAULT_WEIGHTS, trimmed to the factors RouterX can actually
# observe from `RequestLog` + `Model` (quota/health/cost/latency/reliability/
# quality/stability). Sums to 1.0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "health": 0.30,
    "reliability": 0.20,
    "latency": 0.20,
    "cost": 0.15,
    "quality": 0.10,
    "stability": 0.05,
}

_SCORING_FIELDS = tuple(DEFAULT_WEIGHTS)


def normalize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Clamp non-negative weights and rescale to sum 1; fall back to defaults."""
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    cleaned = {k: max(0.0, float(weights.get(k, 0) or 0)) for k in _SCORING_FIELDS}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in cleaned.items()}


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _health_score(candidate: dict[str, Any]) -> float:
    status = str(candidate.get("status") or "unknown")
    return {"healthy": 1.0, "unknown": 0.6, "cooling": 0.3, "dead": 0.0}.get(status, 0.5)


def _reliability_score(candidate: dict[str, Any]) -> float:
    """1 - failure rate over the observed window; 1.0 when nothing observed.

    A candidate nobody has called has not failed anything. That is deliberately
    different from `quality`, which is neutral (0.5) with no observations.
    """
    successes = int(candidate.get("successes", 0) or 0)
    failures = int(candidate.get("failures", 0) or 0)
    total = successes + failures
    if total <= 0:
        return 1.0
    return _clamp01(successes / total)


def _latency_score(candidate: dict[str, Any], slowest: float) -> float:
    latency = float(candidate.get("latency_ms", 0) or 0)
    if latency <= 0 or slowest <= 0:
        return 0.5
    return _clamp01(1.0 - (latency / slowest))


def _cost_score(candidate: dict[str, Any], dearest: float) -> float:
    cost = float(candidate.get("cost_per_1m", 0) or 0)
    if cost <= 0:
        return 1.0  # free / unpriced wins the cost factor by default
    if dearest <= 0:
        return 0.5
    return _clamp01(1.0 - (cost / dearest))


def _quality_score(candidate: dict[str, Any]) -> float:
    value = candidate.get("quality")
    if value is None:
        return 0.5
    try:
        return _clamp01(float(value))
    except (TypeError, ValueError):
        return 0.5


def _stability_score(candidate: dict[str, Any]) -> float:
    """Fewer distinct providers in the chain ⇒ less single-provider exposure."""
    depth = int(candidate.get("chain_depth", 1) or 1)
    return _clamp01(1.0 - min(depth - 1, 4) / 4)


def score_candidates(candidates: Sequence[dict[str, Any]],
                     weights: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Weighted score for each candidate; returns copies sorted best-first.

    Ties break on provider priority then exposed id, so ordering is stable and
    reproducible — the same pool always routes the same way.
    """
    w = normalize_weights(weights)
    if not candidates:
        return []
    slowest = max((float(c.get("latency_ms", 0) or 0) for c in candidates), default=0.0)
    dearest = max((float(c.get("cost_per_1m", 0) or 0) for c in candidates), default=0.0)

    scored: list[dict[str, Any]] = []
    for c in candidates:
        factors = {
            "health": _health_score(c),
            "reliability": _reliability_score(c),
            "latency": _latency_score(c, slowest),
            "cost": _cost_score(c, dearest),
            "quality": _quality_score(c),
            "stability": _stability_score(c),
        }
        total = sum(factors[k] * w[k] for k in _SCORING_FIELDS)
        entry = dict(c)
        entry["score"] = round(total, 6)
        entry["factors"] = {k: round(v, 4) for k, v in factors.items()}
        scored.append(entry)

    scored.sort(key=lambda e: (-e["score"], int(e.get("provider_priority", 100) or 100),
                               str(e.get("exposed_id", ""))))
    return scored


def select(candidates: Sequence[dict[str, Any]], strategy: str = "priority", *,
           weights: dict[str, float] | None = None,
           exclude: Iterable[str] = (),
           last_known_good: str | None = None,
           sla: dict[str, Any] | None = None,
           rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Order a candidate pool for dispatch according to `strategy`.

    Always returns a full ordering (the gateway walks it as its fallback chain),
    never just a winner — that is what makes the first failure recoverable.
    """
    skip = {str(e) for e in exclude}
    pool = [dict(c) for c in candidates if str(c.get("exposed_id", "")) not in skip]
    if not pool:
        return []
    strategy = (strategy or "priority").strip().lower()
    rnd = rng or random

    if strategy == "priority":
        pool.sort(key=lambda c: (int(c.get("stage", 0) or 0),
                                 int(c.get("provider_priority", 100) or 100),
                                 str(c.get("exposed_id", ""))))
        return pool

    if strategy == "weighted":
        return _weighted_order(pool, rnd)

    if strategy == "round-robin":
        return _round_robin_order(pool)

    if strategy == "random":
        rnd.shuffle(pool)
        return pool

    if strategy == "least-used":
        pool.sort(key=lambda c: (int(c.get("req_count", 0) or 0),
                                 -score_candidates([c], weights)[0]["score"],
                                 str(c.get("exposed_id", ""))))
        return pool

    if strategy == "cost-optimized":
        pool.sort(key=lambda c: (float(c.get("cost_per_1m", 0) or 0),
                                 int(c.get("stage", 0) or 0),
                                 str(c.get("exposed_id", ""))))
        return pool

    if strategy == "cost":
        pool.sort(key=lambda c: (float(c.get("cost_per_1m", 0) or 0), str(c.get("exposed_id", ""))))
        return pool

    if strategy == "latency":
        pool.sort(key=lambda c: (float(c.get("latency_ms", 0) or 0) or math.inf,
                                 str(c.get("exposed_id", ""))))
        return pool

    if strategy == "sla":
        return _sla_order(pool, sla or {})

    if strategy == "lkgp":
        return _lkgp_order(pool, last_known_good)

    return score_candidates(pool, weights)


def _weighted_order(pool: list[dict[str, Any]], rnd: random.Random) -> list[dict[str, Any]]:
    """Smooth weighted round-robin (nginx `least_conn` flavour).

    Deterministic on the current selection state, so a burst of concurrent
    requests spreads across keys instead of all landing on the heaviest one.
    """
    remaining = list(pool)
    order: list[dict[str, Any]] = []
    current = {str(c.get("exposed_id", "")): 0.0 for c in pool}
    while remaining:
        best = max(remaining, key=lambda c: (
            int(c.get("weight", 1) or 1) - current[str(c.get("exposed_id", ""))],
            str(c.get("exposed_id", "")),
        ))
        key = str(best.get("exposed_id", ""))
        total = sum(int(c.get("weight", 1) or 1) for c in remaining) or 1
        current[key] += int(best.get("weight", 1) or 1) / total
        order.append(best)
        remaining.remove(best)
    return order


_ROUND_ROBIN_STATE: dict[str, int] = {}
_RR_LOCK = threading.Lock()


def _round_robin_order(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rotate the pool by a per-key cursor so successive calls start elsewhere."""
    pool = sorted(pool, key=lambda c: str(c.get("exposed_id", "")))
    if len(pool) < 2:
        return pool
    key = "|".join(str(c.get("exposed_id", "")) for c in pool)
    with _RR_LOCK:
        start = _ROUND_ROBIN_STATE.get(key, 0) % len(pool)
        _ROUND_ROBIN_STATE[key] = (start + 1) % len(pool)
    return pool[start:] + pool[:start]


def _sla_order(pool: list[dict[str, Any]], sla: dict[str, Any]) -> list[dict[str, Any]]:
    """Satisfying candidates first, then the rest; optionally drop the violators."""
    target_ms = float(sla.get("target_p95_ms", 0) or 0)
    max_error = float(sla.get("max_error_rate", 1.0) if sla.get("max_error_rate") is not None else 1.0)
    max_cost = float(sla.get("max_cost_per_1m", 0) or 0)
    hard = bool(sla.get("hard_constraints", False))

    ok, bad = [], []
    for c in pool:
        latency = float(c.get("latency_ms", 0) or 0)
        error_rate = 1.0 - _reliability_score(c)
        cost = float(c.get("cost_per_1m", 0) or 0)
        within = (not target_ms or latency <= target_ms) and \
                 (not max_cost or cost <= max_cost) and \
                 (max_error >= 1.0 or error_rate <= max_error)
        (ok if within else bad).append(c)

    ok.sort(key=lambda c: (float(c.get("latency_ms", 0) or 0), float(c.get("cost_per_1m", 0) or 0)))
    bad.sort(key=lambda c: (float(c.get("latency_ms", 0) or 0), str(c.get("exposed_id", ""))))
    return ok + ([] if hard else bad)


def _lkgp_order(pool: list[dict[str, Any]], last_known_good: str | None) -> list[dict[str, Any]]:
    """Last known good first (still in the pool), then the normal scoring order."""
    if not last_known_good:
        return score_candidates(pool)
    rest = [c for c in pool if str(c.get("exposed_id", "")) != last_known_good]
    head = [c for c in pool if str(c.get("exposed_id", "")) == last_known_good]
    return head + score_candidates(rest)


# --------------------------------------------------------------------------- #
# The plan — everything above, applied to one request
# --------------------------------------------------------------------------- #

def build_plan(candidates: Sequence[dict[str, Any]], *,
               requested: str = "",
               strategy: str = "priority",
               weights: dict[str, float] | None = None,
               policies: Sequence[dict[str, Any]] | None = None,
               chains: dict[str, list[dict[str, Any]]] | None = None,
               denylist: Iterable[str] | None = None,
               allowlist: Iterable[str] | None = None,
               lockouts: LockoutRegistry | None = None,
               request_tags_: Sequence[str] = (),
               tag_mode: str = "any",
               last_known_good: str | None = None,
               sla: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn a raw candidate pool into a dispatch plan with an audit trail.

    Order of operations, each step able to drop candidates and always saying why:

      1. policy verdict  — a blocked model fails the whole request (403-ish),
      2. exposure lists  — denylist/allowlist globs (advertisement + dispatch),
      3. tag routing     — `metadata.tags` narrows the pool,
      4. lockouts        — a cooling provider/model is skipped, not retried,
      5. strategy        — the survivors are ordered as the fallback chain.

    `steps` explains every exclusion so the dashboard can show *why* a request
    did not use the provider the operator expected.
    """
    lockouts = lockouts or LOCKOUTS
    steps: list[dict[str, Any]] = []
    pool = [dict(c) for c in candidates]

    # 1. policy engine ----------------------------------------------------- #
    verdict = evaluate_policies(policies or [], requested)
    if not verdict["allowed"]:
        return {
            "allowed": False,
            "candidates": [],
            "verdict": verdict,
            "steps": steps,
            "excluded": {},
        }

    # 2. exposure lists ---------------------------------------------------- #
    kept: list[dict[str, Any]] = []
    for c in pool:
        if not is_exposure_allowed(
            str(c.get("exposed_id", "")),
            str(c.get("provider_key", "")),
            denylist=denylist, allowlist=allowlist,
        ):
            steps.append({"exposed_id": c.get("exposed_id", ""), "stage": "exposure",
                          "reason": "hidden by the model exposure list"})
            continue
        kept.append(c)
    pool = kept

    # 3. tag routing -------------------------------------------------------- #
    if request_tags_:
        kept = []
        for c in pool:
            if matches_tags(normalize_tags(c.get("tags")), request_tags_, tag_mode):
                kept.append(c)
            else:
                steps.append({"exposed_id": c.get("exposed_id", ""), "stage": "tags",
                              "reason": f'tags {list(request_tags_)} ({tag_mode}) did not match'})
        pool = kept

    # 4. lockouts ----------------------------------------------------------- #
    kept = []
    for c in pool:
        provider_key = str(c.get("provider_key", ""))
        model_id = str(c.get("model_id", ""))
        if lockouts.is_locked(provider_key, model_id):
            steps.append({
                "exposed_id": c.get("exposed_id", ""), "stage": "lockout",
                "reason": f"cooling down for {lockouts.remaining_ms(provider_key, model_id)}ms",
            })
            continue
        kept.append(c)
    pool = kept

    # 5. strategy ----------------------------------------------------------- #
    ordered = select(pool, strategy, weights=weights, last_known_good=last_known_good, sla=sla)

    excluded: dict[str, int] = {}
    for step in steps:
        excluded[step["stage"]] = excluded.get(step["stage"], 0) + 1

    return {
        "allowed": True,
        "candidates": ordered,
        "verdict": verdict,
        "steps": steps,
        "excluded": excluded,
        "strategy": strategy,
    }


# --------------------------------------------------------------------------- #

def _now_ms() -> int:
    return int(time.time() * 1000)


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


__all__ = [
    "CFG_POLICIES", "CFG_FALLBACKS", "CFG_LOCKOUTS", "CFG_ROUTER", "CFG_EXPOSURE",
    "STRATEGIES", "COMBO_STRATEGIES", "ROUTER_STRATEGIES", "DEFAULT_WEIGHTS",
    "LOCKOUTS", "LockoutRegistry", "ClientLockouts",
    "normalize_tags", "normalize_match_mode", "matches_tags", "request_tags",
    "load_policies", "evaluate_policies",
    "load_chains", "resolve_chain", "next_fallback", "has_fallback",
    "normalize_weights", "score_candidates", "select", "build_plan",
]
