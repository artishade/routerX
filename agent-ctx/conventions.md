# RouterX — Coding Conventions & Invariants (Python era)

Rules that keep changes consistent. Break these only with a documented reason
(then record it in `decisions.md`).

## Language & stack

1. **Python everywhere.** No Node build step, no React, no new JS frameworks.
   `static/app.js` is the only hand-written frontend JS. The engine sidecar
   (`engine/index.js`) must stay zero-npm-dependency bun/node code.
2. Docstrings at the top of every module explaining what it owns (existing style).
3. Type hints with `from __future__ import annotations`; `dict[str, Any]` style
   generics; `str | None` unions.

## Database (SQLAlchemy 2)

4. `nova/models.py` is the single source of schema truth. Never hand-edit the
   DB file; never write destructive migrations. `nova/bootstrap.py` creates
   missing tables and adds missing columns (constant defaults, no NOT NULL) at boot.
5. Request-scoped DB access uses the `get_db()` dependency. Long-lived work
   (agent loop, background tasks) opens its own `SessionLocal()` per operation —
   never hold one session across a whole task run.
6. SystemConfig is the KV table — simple config knobs go through `nova/kv.py`
   helpers instead of new columns.
7. Prisma-style `DATABASE_URL` values are normalized in `nova/config.py`
   (`file:…`, `postgres://`, pgbouncer params). Keep that tolerant.

## API conventions

8. **JSON wire format is snake_case**; DB columns/rows are the ORM's business.
   Keep parity with `agent-ctx/api-contract.md` (historical Next.js contract,
   still the shape reference).
9. Gateway endpoints exist under **both** `/v1/*` and `/api/v1/*` (mount the same
   router twice in `main.py`).
10. Admin APIs under `/api/admin/*` return `{ok: true}` style responses for
    mutations and use `routers/_common.py` helpers (`json_body`, `not_found`,
    `invalid_id`, `mask_key`, `epoch_ms`).
11. Errors are JSON `{error: "message"}` with correct 4xx/5xx status codes.
    A 4xx/5xx from the gateway must also write a RequestLog row. Provider/engine
    faults are 502/503/504 (Anthropic-shaped on `/v1/messages`), never a bare
    500, and every gateway response carries `x-request-id`.
12. Timeouts for upstream calls are env-tunable (`NOVA_CONNECT_TIMEOUT`,
    `NOVA_UPSTREAM_READ_TIMEOUT`, `NOVA_NONSTREAM_READ_TIMEOUT`) and share pooled
    `httpx.AsyncClient` instances — never build a fresh client per request.
13. Never expose real API keys; use `mask_key()` (first 8 + … + last 4).

## Gateway invariants (do not break)

14. Response `model` field ALWAYS equals the requested id (identity spoofing);
    the truth goes in `_nova` metadata (`upstream_model`, `provider`, `stage`,
    `fallback`, `spoofed`).
15. Fallback order: Stage 1 direct upstream → Stage 2..N explicit route chain →
    final RouterX Free engine. The engine degrades honestly (502, never fabricated).
16. `nova/*` tier aliases (`routerx/pro`, `routerx/air`, `routerx/mini`) resolve via
    `nova/freemodels.py` onto the free-models catalogue.
17. Every gateway request — success or failure — is logged (`RequestLog` via
    `gwlog.record_request`).

## UI conventions (Jinja2 + HTMX)

18. Tabs are server-rendered fragments swapped into `#tab-content` by HTMX; a
    tab module owns `GET /partials/tab/<key>` + optional `POST /ui/<key>/...`
    fragment-returning actions. Fragments, not JSON, drive UI updates.
19. Theme: matte black — bg `#050505`, panels `#0b0b0b/80` + neutral borders,
    **darkest-red** interaction accent, green success/health (`green-*`), amber
    warnings, rose errors, purple reserved for RAM/heap/spoof accents, sky only
    for tiny info chips, mono for ids/commands. The `emerald-*` utilities are
    the accent class name: the palette is remapped to the RouterX red ramp in
    `templates/base.html`, and `slate-*` is remapped to a neutral ramp so no
    surface keeps the old navy cast. **No indigo/blue**, no emojis in the UI.
20. aria-labels on icon-only buttons; status dots for healthy/cooling/dead/
    unknown; skeletons or graceful empty states when APIs fail (tabs must not
    blank out).
21. New config surfaced to the UI goes through `/api/admin/meta` (MetaConfig).

## Terminal duality (easy to confuse)

22. **Command mode** (one-shot exec, history, `$ cmd` in console) =
    `terminal/sandbox.py` — simulated allowlist executor, NO child processes
    (only exception: `--version` probes of node/bun/npm binaries on PATH).
    Dangerous tokens exit 126, unknown exit 127, history capped at 200 rows.
23. **Session tabs** (interactive bash, ssh, vim) = `terminal/pty.py` —
    real PTYs, max 8 concurrent, 30-min idle reap, `close_all()` on shutdown.
24. When touching terminal behavior, first decide which of the two it belongs
    to. Sandbox policy changes go in `terminal/sandbox.py`; session lifecycle in
    `terminal/pty.py` + `terminal/link.py` (the dashboard glue in
    `routers/admin_terminal.py` just mounts those routes and adds DB history).
25. **Everything terminal lives under `terminal/`**, imported from there as
    `terminal.*` — and the dependency is one-way *all the way*: `terminal/`
    imports nothing from `nova/`. It reads the environment itself
    (`terminal/config.py`), which is what lets the terminal be hosted on its own
    (`docker build ./terminal`, `python3 -m terminal.service`). Don't "tidy"
    a nova import back in there; the only module allowed to borrow the app is
    `sandbox.py`, and it guards every entry point with `APP_AVAILABLE`.
26. **A separately hosted terminal still gets the AI agent.** Agentbox
    (`terminal/agentbox.py`, mounted at `/agent` only by the service — the
    gateway has its own `/api/agent/*`) is configured by env and otherwise
    reports `configured: false` rather than pretending. Its tools go through
    the link, so they land in real PTY tabs on whichever host runs them.

## Testing & verification

25. Full offline check: `sh scripts/test.sh` (byte-compile → unittest → node).
    Quick syntax lint: `python3 -m compileall -q main.py nova routers ui`.
    Targeted run: `python3 -m unittest tests.test_<name> -v`.
26. Provider protocol tests use httpx MockTransport; the gateway needs no keys.
    Keep tests offline — no network in CI.
27. Server binds `0.0.0.0:$PORT` (never hardcode); the engine sidecar never
    shares the app's port (`NOVA_ENGINE_PORT`, default 3099).

## Process discipline

28. `worklog.md` + `agent-ctx/api-contract.md` are historical Next.js documents —
    read-only reference, do not "fix" them to match Python. Current memory lives
    in `AGENTS.md` + `agent-ctx/*.md` (keep those fresh instead).
29. Bootstrap never seeds mock/demo data; legacy demo rows are cleaned on boot.
30. Additive-only schema changes; new deploys must never wipe user data
    (that's why Postgres `DATABASE_URL` is recommended and SQLite is flagged
    as ephemeral in `/health`).
