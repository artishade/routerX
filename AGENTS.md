# AGENTS.md — RouterX agent memory

> **READ THIS FIRST.** Always-loaded memory. Deeper context in `agent-ctx/`
> (start with `project-map.md`). Skill `nova-architect` loads the analysis workflow.

## ⚠️ Current state

- **100% Python (FastAPI + Jinja2 + HTMX).** No Next.js/React/Prisma anywhere.
- `worklog.md` + `agent-ctx/api-contract.md` are a **historical Next.js rebuild** used as an API-shape reference only — their paths (`src/app/api/**`) do not exist. Python equivalents: `project-map.md`.
- **No third-party attribution.** RouterX is its own design. Do not name other projects in docs/UI. The catalogue is curated (`nova/data/provider_registry.json`, `base_url` is always an API root).

## What RouterX is

Self-hosted AI gateway: fans requests across providers (keyless/free included) with fallback chains, cooldowns, key/model/route management, client-key auth, rate limits, an **11-tab** dashboard (overview, playground, console, providers, models, routes, keys, storage, analytics, logs, terminal), a sandbox terminal and an autonomous agent. One FastAPI process serves the gateway API, the admin/agent JSON APIs and the UI.

## Repo map

| Path | Role |
| --- | --- |
| `main.py` | Entrypoint: lifespan (bootstrap, engine sidecar, maintenance), CORS, `/health`, router mounting, request-log middleware |
| `nova/` | Core: config, models, bootstrap, engine client, transport, discovery/sync, agent, storage, routing brain (`routing`, `router_settings`, `profiles`), gateway control (`auth`, `ratelimit`, `cache`, `session`), catalogue (`catalog`, `registry`, `data/*.json`) |
| `terminal/` | Whole terminal feature, self-contained (no nova import, no DB): `pty.py`, `sandbox.py`, `link.py`, `api.py` (shared contract), `agentbox.py` (`/agent/*`), `web/`, `service.py`, `config.py`, `Dockerfile` |
| `routers/` | `gateway.py` (`/v1` + `/api/v1`, incl. `openai_responses.py`), `admin_*.py` (`/api/admin`, incl. `admin_limits.py`), `agent_api.py`, `anthropic_adapter.py`. `admin_terminal.py` = dashboard glue only |
| `ui/` | BFF frontend: `shell.py`, `tabs/` (11 tab modules incl. `playground.py`), `api_client.py`, `render.py`, `format.py` |
| `templates/` | Jinja2: `base.html`, `tabs/<key>.html`, `partials/*.html` |
| `static/` | `nova.css`, `nova-world.css` (Dark AI World layer), `app.js` (only hand-written app JS), `terminal_workspace.js`, `logo.svg` |
| `engine/` | RouterX Free sidecar (`index.js`, zero npm deps) + `free-models.json` |
| `tests/`, `scripts/`, `db/`, `agent-ctx/` | unittest + `.cjs` suites; `preview.sh`/`test.sh`; SQLite location; agent memory |

## Architecture in one paragraph

`main.py` boots FastAPI → lifespan runs `nova/bootstrap.py` (additive schema sync + seed of the curated catalogue) and starts the engine sidecar (`nova/engine.py`) → gateway routes pass through `nova/auth.py` `gateway_auth` → `routers/gateway.py` resolves virtual ids (`auto`, `auto:<profile>` via `nova/profiles.py`), asks `nova/routing.py` to order the candidate pool (policy engine → exposure lists → tag routing → per-(provider, model) lockouts/usage caps → strategy), applies sticky-session reorder (`nova/session.py`), then walks it as a fallback chain (direct → chain → RouterX Free engine last), calling upstreams through `nova/provider_transport.py` + pooled `httpx`; key cooldowns/usage booking via `nova/ratelimit.py`; optional `nova/cache.py`. Response `model` is spoofed to the requested id, truth in `_nova`, every request logged to `RequestLog` (with `ttftMs`). Dashboard: server-rendered Jinja2 fragments swapped by HTMX, each tab calling the app's own JSON API (`ui/api_client.py`). `nova/agent.py` runs plan→tool→step loops. `terminal/` never imports `nova/`.

## Non-negotiable conventions

1. **Python only** — no Node build, no React. `static/app.js` is the only hand-written app JS.
2. **DB**: SQLAlchemy 2, `nova/models.py` is schema truth. `get_db()` for request scope; long tasks open own `SessionLocal()`. Bootstrap strictly **additive** (missing tables/columns only); never destructive, never mock data.
3. **Wire format**: JSON is **snake_case**; DB rows are camelCase columns. Shape reference: `agent-ctx/api-contract.md`.
4. **Gateway invariants**: response `model` = requested id; truth in `_nova`; every request writes a `RequestLog`; endpoints under both `/v1/*` and `/api/v1/*`; routing decisions are pure in `nova/routing.py`, never in a router. Provider faults = typed 502/503/504 + `x-request-id`, never bare 500; 2xx bodies validated.
5. **Auth is fail-closed once a client key exists**, fail-open on a fresh install (`NOVA_REQUIRE_API_KEY` forces keys). Every gateway route depends on `nova_auth.gateway_auth`.
6. **Opt-in only**: rate limits default to unlimited (`0`); response cache OFF; sticky sessions only when `x-nova-session` is sent. A default must never change routing for an untouched deployment.
7. **Style**: matte black `#050505`, panels `#0b0b0b/80`, **darkest-red** accent (`emerald-*` utilities remapped to the RouterX red ramp in `templates/base.html`), `green-*` success/health, amber warnings, rose errors, purple reserved for RAM/spoof; `slate-*` remapped to neutral. Look lives in `static/nova-world.css` (aurora, reveal-on-scroll, hover glow) and must respect `prefers-reduced-motion`. No emojis in UI; aria-labels on icon buttons.
8. **Ports**: `0.0.0.0:$PORT` (default 3000). Engine sidecar `NOVA_ENGINE_PORT` (3099), never the app port.
9. **Tests**: `sh scripts/test.sh` (compileall → `python3 -m unittest discover -s tests -p 'test_*.py'` → `.cjs` suites). Fast lint: `python3 -m compileall -q main.py nova routers ui terminal`. Offline; MockTransport.
10. **Terminal duality**: command exec = `terminal/sandbox.py` (simulated, no subprocess); session tabs = `terminal/pty.py` (real PTY, max 8, 30-min reap). Don't mix them.
11. **Terminal is one path**: all terminal code in `terminal/`, imported as `terminal.*`, importing **nothing** from `nova/` (except `sandbox.py`, guarded by `APP_AVAILABLE`). Hostable alone (`python3 -m terminal.service`, `docker build ./terminal`).

## Where to add things

- Admin JSON endpoint → `routers/admin_*.py`, mounted in `main.py` under `/api/admin`.
- Gateway capability → `routers/gateway.py` (+ wire helpers in `nova/provider_transport.py`); new protocol adapter = sibling module like `routers/openai_responses.py`.
- New dashboard tab → `ui/tabs/<key>.py` (`router`) + register in `ui/tabs/__init__.py` + `templates/tabs/<key>.html`.
- Config knob → env var in `nova/config.py`, exposed via `/api/admin/meta` if UI-relevant.
- Usage/limit knob → `nova/ratelimit.py` (+ `SystemConfig` `rate_limits`), surfaced via `routers/admin_limits.py`.
- Routing rule → `nova/routing.py` (+ `nova/router_settings.py` knob), surfaced via `routers/admin_routing.py`.
- Routing profile → `nova/profiles.py` (+ `routers/admin_routing.py` profiles routes).
- Agent tool → `nova/agent.py` `ALLOWED_ACTIONS` + executor + `routers/agent_api.py` list.
- Provider/model source → `nova/data/*.json` behind `nova/catalog.py`, validated by `nova/registry.py` (`base_url` = API root).
- Recipes with skeletons → `agent-ctx/recipes.md`.
