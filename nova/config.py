"""RouterX Python core — configuration and environment handling.

The server binds to 0.0.0.0:$PORT (Render assigns PORT dynamically — never
hardcoded). DATABASE_URL accepts Prisma-style values (`file:…`, `postgres://`,
pgbouncer query params) and normalizes them for SQLAlchemy.

Terminal configuration is not here: everything terminal-related lives under
`terminal/` and is imported from there (`terminal.config`, `terminal.link`, …).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Public base URL (requirement #2)
# --------------------------------------------------------------------------- #
# Every externally-visible base_url reference (README examples, .env.example,
# the meta descriptor served to the dashboard) points at the hosted domain.
HOSTED_DOMAIN = "https://novarouter.onrender.com"

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# The client key the dashboard (ui/api_client.py) presents to its own gateway.
# Needed only once client-key auth is active; unset keeps anonymous loopback
# calls, which a fresh install still allows.
UI_GATEWAY_TOKEN = os.environ.get("NOVA_UI_GATEWAY_TOKEN", "").strip()


def public_base_url(request_base: str | None = None) -> str:
    """Best-known public origin of this deployment.

    Priority: PUBLIC_BASE_URL env → incoming request origin (correct for any
    host, including the Render domain) → the hosted domain constant.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if request_base:
        return request_base.rstrip("/")
    return HOSTED_DOMAIN


# --------------------------------------------------------------------------- #
# Database URL normalization
# --------------------------------------------------------------------------- #

_PRISMA_QUERY_KEYS = {"pgbouncer", "connection_limit", "pool_timeout", "sslaccept", "sslmode"}


def _postgres_dialect() -> str | None:
    """Explicitly resolve an installed psycopg driver.

    SQLAlchemy >= 2.1 resolves a plain `postgresql://` URL to the psycopg3
    dialect and crashes at import time when psycopg3 is not installed — even
    when psycopg2 is present. Pinning the dialect keeps the same DATABASE_URL
    working everywhere (local, Docker image, Render) regardless of the
    SQLAlchemy minor version.
    """
    try:
        import psycopg2  # noqa: F401

        return "postgresql+psycopg2"
    except ImportError:
        pass
    try:
        import psycopg  # noqa: F401

        return "postgresql+psycopg"
    except ImportError:
        return None


# Absolute-path detection for a `file:` payload: POSIX (`/abs/x.db`), Windows
# drive letters (`C:/x.db`, `C:\x.db`) and UNC shares (`//host/share/x.db`).
# A bare `C:`/`file:` scheme prefix is NOT a path here - `re` and `http` are
# not drives either.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_absolute_sqlite_path(path: str) -> bool:
    """True when a `file:` payload is already an absolute SQLite path."""
    if not path:
        return False
    if path.startswith(("/", "\\")):
        return True
    return bool(_WINDOWS_DRIVE.match(path))


def normalize_database_url(raw: str | None) -> str:
    """Translate Prisma-style DATABASE_URL values into SQLAlchemy URLs."""
    url = (raw or "").strip()
    if not url:
        return f"sqlite:///{(PROJECT_ROOT / 'db' / 'custom.db').as_posix()}"

    # Prisma SQLite form: file:./db/custom.db | file:../db/custom.db | file:/abs/path.db
    # Windows absolute paths (file:C:/db/x.db, file:C:\db\x.db) and UNC shares
    # (file://server/share/x.db) must survive too: they are already absolute on
    # their own platform, so they are never joined onto PROJECT_ROOT.
    if url.startswith("file:"):
        path = url[5:]
        # `file:///abs/my.db` is valid Prisma too; SQLAlchemy reads
        # `sqlite:////abs/my.db` (four slashes) as the absolute POSIX path.
        if path.startswith("///"):
            path = path[2:]
        if _is_absolute_sqlite_path(path):
            return f"sqlite:///{path}"
        return f"sqlite:///{str((PROJECT_ROOT / path).resolve())}"

    parsed = urlparse(url)
    if parsed.scheme == "postgres":
        parsed = parsed._replace(scheme="postgresql")

    # Drop Prisma-specific pool params that psycopg does not understand.
    if parsed.scheme.startswith("postgresql"):
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _PRISMA_QUERY_KEYS]
        parsed = parsed._replace(query=urlencode(query))
        url = urlunparse(parsed)
        # Pin the dialect only when the URL has no explicit +driver part.
        if parsed.scheme == "postgresql":
            dialect = _postgres_dialect()
            if dialect:
                url = f"{dialect}://{url.split('://', 1)[1]}"
        return url

    if parsed.scheme == "sqlite":
        return _normalize_sqlite_url(url)
    return urlunparse(parsed)


def _normalize_sqlite_url(url: str) -> str:
    """Canonical sqlite URL for an explicit `sqlite:` URL.

    `urlparse`/`urlunparse` collapse the third slash of `sqlite:///...` (sqlite
    has no netloc), so a Windows drive path (`sqlite:///C:/x.db`) used to come
    back as the unopenable `sqlite:/C:/x.db`. Rebuilding the URL from the raw
    payload keeps every platform's paths intact.

    Slash conventions (never add a slash - that corrupts a drive path):

      sqlite:///C:/x.db   three slashes + drive  -> unchanged (absolute on Windows)
      sqlite:///rel.db    three slashes          -> unchanged (relative)
      sqlite:////abs.db   four slashes           -> unchanged (POSIX absolute)
      sqlite://rel.db     two slashes (no host)  -> sqlite:///rel.db
      sqlite:/C:/x.db     no `//` netloc marker  -> sqlite:///C:/x.db

    Legacy four-slash drive paths (`sqlite:////C:/x.db`, produced by the old
    force-a-leading-slash version) are repaired back to three slashes.
    """
    scheme, sep, rest = url.partition(":")
    if not sep:
        return url
    rest, _, query = rest.partition("?")
    if rest.startswith("//"):
        rest = rest[2:]                      # drop the empty-netloc `//`
        path = rest[1:] if rest.startswith("/") else rest   # drop one of the 3 slashes
    elif rest.startswith("/"):
        path = rest[1:]                      # `sqlite:/C:/x.db` (libpq style)
    else:
        path = rest
    # A POSIX-absolute path keeps its two leading slashes here (`//abs/x.db`);
    # a drive path (`C:/x.db`) is absolute on its own, so drop legacy leading
    # slashes that would turn it into `//C:/x.db`.
    if path.startswith("/") and _WINDOWS_DRIVE.match(path.lstrip("/")):
        path = path.lstrip("/")
    return f"{scheme}:///{path}" + (f"?{query}" if query else "")


DATABASE_URL_RAW = os.environ.get("DATABASE_URL", "")
DATABASE_URL = normalize_database_url(DATABASE_URL_RAW)
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

# --------------------------------------------------------------------------- #
# Gateway auth / limits / cache knobs (additive - every default keeps the
# pre-existing behaviour, so an untouched deployment routes exactly as before)
# --------------------------------------------------------------------------- #
# Client-key auth (nova/auth.py). Anonymous requests stay allowed while there
# are no enabled ClientKey rows; set this truthy to require a key regardless.
REQUIRE_API_KEY = os.environ.get("NOVA_REQUIRE_API_KEY", "").strip().lower() in ("1", "true", "yes", "on")


# Per-(provider, model) rate/usage caps (nova/ratelimit.py). 0 = unlimited and
# is the default: the catalogue-free deployment must not change routing.
#   NOVA_DEFAULT_RPM / TPM  - per-minute requests / tokens
#   NOVA_DEFAULT_RPD / TPD  - per-day   requests / tokens
# Per-provider overrides live in the `rate_limits` SystemConfig JSON knob.
def _env_int(name: str, default: int = 0) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


DEFAULT_RPM = _env_int("NOVA_DEFAULT_RPM", 0)
DEFAULT_TPM = _env_int("NOVA_DEFAULT_TPM", 0)
DEFAULT_RPD = _env_int("NOVA_DEFAULT_RPD", 0)
DEFAULT_TPD = _env_int("NOVA_DEFAULT_TPD", 0)

# Upstream key cooldown defaults (nova/ratelimit.py cooldown_seconds).
KEY_COOLDOWN_429_S = float(os.environ.get("NOVA_KEY_COOLDOWN_429", "60") or 60)
KEY_COOLDOWN_5XX_S = float(os.environ.get("NOVA_KEY_COOLDOWN_5XX", "30") or 30)
KEY_COOLDOWN_401_S = float(os.environ.get("NOVA_KEY_COOLDOWN_401", "300") or 300)
KEY_COOLDOWN_MAX_S = float(os.environ.get("NOVA_KEY_COOLDOWN_MAX", "900") or 900)

# Opt-in response cache (nova/cache.py). OFF unless one of these says on.
RESPONSE_CACHE = os.environ.get("NOVA_RESPONSE_CACHE", "").strip().lower() in ("1", "true", "yes", "on")
RESPONSE_CACHE_TTL_S = _env_int("NOVA_RESPONSE_CACHE_TTL", 300)
RESPONSE_CACHE_MAX = _env_int("NOVA_RESPONSE_CACHE_MAX", 256)

# Sticky sessions (nova/session.py): how long a conversation stays pinned to the
# model that served its first turn when the client sends `x-nova-session`.
SESSION_TTL_S = _env_int("NOVA_SESSION_TTL", 1800)


# --------------------------------------------------------------------------- #
# Runtime tuning (V8-heap equivalents are kept as gateway config keys)
# --------------------------------------------------------------------------- #

PORT = int(os.environ.get("PORT", "3000"))  # Render/Freebuff inject PORT dynamically

# The engine sidecar must never share the HTTP port. Freebuff Cloud points its
# preview proxy at that single port, so a second listener there makes the proxy
# answer with the sidecar's 404 instead of the dashboard. 3099 stays the default
# everywhere else (Docker, Render); it only steps aside when the app is on it.
ENGINE_SIDECAR_PORT = int(os.environ.get("NOVA_ENGINE_PORT") or (3099 if PORT != 3099 else 3100))
ENGINE_SIDECAR_URL = f"http://127.0.0.1:{ENGINE_SIDECAR_PORT}"

# CORS (requirement #4): browser clients may call the API directly.
CORS_ALLOW_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")
    if o.strip()
]

# NOTE: the terminal's own knobs (NOVA_TERMINAL_PORT / _URL / _TOKEN) live in
# `terminal/config.py` — the terminal can be hosted separately, so its settings
# belong to the terminal, not to the app that connects to it.

def engine_runtime() -> str | None:
    """Locate a JS runtime able to host the dependency-free engine sidecar."""
    from shutil import which

    for candidate in ("bun", "node"):
        if which(candidate):
            return candidate
    return None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "provider"
