# AGENTS.md — RouterX agent memory

> **READ THIS FIRST.** This file is the always-loaded memory for AI coding agents
> working on RouterX. Deeper context lives in `agent-ctx/` (start with
> `agent-ctx/project-map.md`). The skill `nova-architect` (`.agents/skills/`)
> loads the full analysis workflow for feature customization work.

## ⚠️ Current state of this repo

- **The app is 100% Python (FastAPI + Jinja2 + HTMX).** There is NO Next.js/React/Prisma code in this repo.
- `worklog.md` and `agent-ctx/api-contract.md` describe a **historical Next.js rebuild** used as a porting reference. Treat them as read-only history — their file paths (`src/app/api/**`, `src/lib/**`) do not exist here. The Python equivalents are noted in `agent-ctx/project-map.md`.

## What RouterX is

A self-hosted AI API gateway: fans requests out across many AI providers (free ones included), with fallback chains, cooldowns, key/model/route management, a 9-tab dashboard, a sandbox terminal, and an autonomous agent. One FastAPI process serves the gateway API, the admin/agent JSON APIs, and the dashboard UI.

## Repo map (one screen)

| Path | Role |
| --- | --- |
| `main.py` | FastAPI entrypoint: lifespan (bootstrap, engine sidecar, maintenance), CORS, `/health`, router mounting, gateway request-log middleware |
| `nova/` | Core library (config, SQLAlchemy models, bootstrap, engine sidecar client, provider transport, discovery, sync, agent runtime, storage, routing brain) |
| `terminal/` | The whole terminal feature in one path, self-contained (no nova import, no DB): `pty.py` (real PTY sessions), `sandbox.py` (allowlist exec), `link.py` (LocalLink/RemoteLink seam), `api.py` (contract mounted by both hosts), `agentbox.py` (AI agent at `/agent/*`), `web/` (console page served at `/`), `service.py` (standalone host), `config.py`, `run.sh`, `Dockerfile` (builds alone) |
| `routers/` | HTTP APIs: `gateway.py` (`/v1/*` + `/api/v1/*`), `admin_*.py` (`/api/admin/*`), `agent_api.py` (`/api/agent/*`), `anthropic_adapter.py`. `admin_terminal.py` is only the dashboard's glue — the terminal's own routes come from `terminal/api.py` |
| `ui/` | Python frontend (BFF): `shell.py` (dashboard shell), `tabs/` (9 tab modules), `api_client.py` (self-API HTTP client), `render.py` (Jinja2), `format.py` |
| `templates/` | Jinja2: `base.html`, `tabs/<key>.html`, `partials/*.html` |
| `static/` | `nova.css`, `app.js` (HTMX wiring, toasts, streaming chat), `logo.svg` |
| `engine/` | RouterX Free engine sidecar (`index.js`, zero npm deps) + `free-models.json` catalogue |
| `tests/` | `tests/test_*.py` (unittest) + JS suite |
| `scripts/` | `preview.sh` (install deps + run), `test.sh` (compileall + unittest + node) |
| `db/` | SQLite file location when no `DATABASE_URL` is set |
| `agent-ctx/` | Agent memory: `project-map.md`, `conventions.md`, `recipes.md`, `decisions.md` |

## Architecture in one paragraph

`main.py` boots FastAPI → lifespan runs `nova/bootstrap.py` (additive schema sync + seed, including the OmniRoute provider/model catalogue) and starts the engine sidecar via `nova/engine.py` → requests hit `routers/gateway.py` (`/v1/chat/completions` etc.), which asks `nova/routing.py` to order the candidate pool (policy engine → model exposure lists → tag routing → per-(provider, model) lockouts → selection strategy), then walks it as a fallback chain (direct → chain → RouterX Free engine last), calls upstreams through `nova/provider_transport.py` + pooled `httpx` clients, spoofs the response `model` to the requested id, attaches `_nova` metadata (including the routing decision), and logs to `RequestLog`. The dashboard (`ui/`) is server-rendered Jinja2 fragments swapped by HTMX; each tab module calls the app's own JSON API over HTTP (`ui/api_client.py`), so UI behaviour always matches the gateway. `nova/agent.py` runs autonomous tasks with a plan→tool→step loop. The terminal is a separate feature under `terminal/` (imported from there as `terminal.*`, and never importing `nova` back): `terminal/sandbox.py` is a simulated allowlist sandbox (no real shells), `terminal/pty.py` spawns the real PTY shells behind the Terminal tab sessions, and `terminal/agentbox.py` is the AI agent that travels with a separately hosted terminal.

## Non-negotiable conventions

1. **Python only** — no Node build, no React, no new JS frameworks. `static/app.js` is the only frontend JS.
2. **DB**: SQLAlchemy 2, `nova/models.py` is the single source of schema truth. Use `get_db()` dependency for request-scoped sessions; long-lived tasks (agent) open their own `SessionLocal()` per operation. Bootstrap is strictly **additive** — it creates missing tables and adds missing columns with constant defaults; never destructive migrations, never seed mock data.
3. **Wire format**: API JSON is **snake_case**; DB models are camelCase-column SQLAlchemy rows. Keep parity with the contract in `agent-ctx/api-contract.md` (historical but still the API shape reference).
4. **Gateway invariants**: response `model` field always equals the requested id (identity spoofing); truth lives in `_nova` metadata; every request writes a `RequestLog` row; endpoints exist under both `/v1/*` and `/api/v1/*`. Routing decisions are pure functions in `nova/routing.py` — never inline a selection rule in a router. A provider/engine fault is a typed stage failure (502/503/504 with `x-request-id`), never a bare 500; 2xx upstream bodies are validated before use.
5. **Style**: matte-black terminal aesthetic — bg `#050505`, panels `#0b0b0b/80` + neutral borders, **darkest-red** interaction accent, green for success/health, amber warnings, rose errors, purple reserved for RAM/spoof accents, mono for ids/commands. The accent is themed in `templates/base.html` (`tailwind.config.theme.extend.colors`): the `emerald` scale is remapped to the RouterX red ramp and `slate` to a neutral ramp, so write accent utilities as `emerald-*` and semantic success as `green-*`. No emojis in UI, aria-labels on icon buttons.
6. **Ports**: server binds `0.0.0.0:$PORT` (default 3000, never hardcode). Engine sidecar is on its own port (`NOVA_ENGINE_PORT`, default 3099, never the app port).
7. **Tests/verify**: `sh scripts/test.sh` (byte-compile → `python3 -m unittest discover -s tests -p 'test_*.py'` → JS suite). Fast lint: `python3 -m compileall -q main.py nova routers ui`. Offline; provider protocol tests use httpx MockTransport.
8. **Terminal duality**: command-mode exec = `terminal/sandbox.py` (simulated allowlist, no subprocess); session tabs = `terminal/pty.py` (real PTY shells, max 8, reaped after 30 min idle). Don't mix them up when changing terminal behavior.
9. **The terminal is one path**: everything terminal-related lives in `terminal/` and the rest of the app imports it as `terminal.*` (`from terminal import link`, `from terminal.api import router`, `from terminal.sandbox import execute_command`). Never reach back from `terminal/` into the app's internals, and never inline terminal behavior in a router — that is what keeps the terminal hostable on its own (`python3 -m terminal.service`, `docker build ./terminal`). `terminal/` must keep importing **nothing** from `nova/`: it is a separate deployable unit with its own config, its own console page (`terminal/web/`, served at `/` so a deployed terminal is usable in a browser) and its own Agentbox (`terminal/agentbox.py`, the AI agent at `/agent/*`). `sandbox.py` is the single exception and degrades honestly when the app isn't attached.

## Where to add things (quick pointers)

- New admin JSON endpoint → `routers/admin_*.py` (pick the domain file), mounted in `main.py` under `/api/admin`.
- New gateway capability → `routers/gateway.py` (+ wire-format helpers in `nova/provider_transport.py`).
- New dashboard tab → module in `ui/tabs/` exporting `router` + register in `ui/tabs/__init__.py` + `templates/tabs/<key>.html`.
- New config knob → env var in `nova/config.py`, surfaced via `/api/admin/meta` if UI-relevant.
- New routing rule → `nova/routing.py` (+ `nova/router_settings.py` for the `SystemConfig` knob), surfaced through `routers/admin_routing.py`.
- New provider/model source → `nova/data/*.json` behind a `nova/catalog.py` accessor.
- Full recipes (with code skeletons) → `agent-ctx/recipes.md`.
