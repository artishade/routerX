# RouterX — Deep Project Map (Python/FastAPI era)

> Python-port reality. The historical Next.js layout (`src/app/api/**`, `src/lib/**`)
> in `api-contract.md` no longer exists in this repo — the Python modules below are
> the ports of those files. Use this map to find code without re-scanning.

## Process topology

```
┌────────────────────────── one process: python3 main.py ──────────────────────────┐
│  FastAPI (0.0.0.0:$PORT)                                                        │
│   ├─ /v1/* + /api/v1/*        routers/gateway.py        (OpenAI-spec gateway)   │
│   ├─ /api/admin/*             routers/admin_*.py        (JSON admin APIs)       │
│   ├─ /api/agent/*             routers/agent_api.py      (agent tasks/tools)     │
│   ├─ / + /partials/*          ui/shell.py + ui/tabs/*   (Jinja2 BFF dashboard)  │
│   └─ /static                  static/ (nova.css, app.js)                        │
│                                                                                  │
│  engine sidecar (separate process): bun/node engine/index.js on 127.0.0.1:3099  │
│   └─ free-model chat stream / search / reader (no API key needed)               │
└──────────────────────────────────────────────────────────────────────────────────┘
```

The UI is a **self-BFF**: `ui/api_client.py` calls this same process's JSON API
over HTTP (`/api/admin/...`), so UI behaviour always matches the gateway. The
gateway log middleware in `main.py` also records every `/v1/*` call to `RequestLog`
(via `nova/gwlog.py`).

## Module-by-module (nova/ — the core library)

| Module | Responsibility / key facts |
| --- | --- |
| `config.py` | Env handling: `PORT`, `DATABASE_URL` normalization (Prisma-style `file:` / `postgres://` accepted), `IS_POSTGRES`, `PUBLIC_BASE_URL`/`public_base_url()`, `ENGINE_SIDECAR_PORT` (3099, steps aside if it collides with app port), `CORS_ALLOW_ORIGINS`, `slugify()` |
| `database.py` | SQLAlchemy engine + `SessionLocal`; `get_db()` FastAPI dependency; `ping()`; `ensure_sqlite_dir()` |
| `models.py` | All ORM rows (single source of schema truth): Provider, ProviderKey, Model, ModelRoute, ClientKey, RequestLog, TerminalCommand, SystemConfig (KV), StorageProviderRow, StorageFile, AgentTask, AgentStep, ProviderSession, McpServer. `utcnow()` helper |
| `bootstrap.py` | First-boot: additive table creation **and additive missing-column sync** + seeds RouterX Free engine + gateway config + the OmniRoute provider/model catalogue. Never destructive, never mock data |
| `catalog.py` | Read-only accessor over `nova/data/provider_registry.json` (the OmniRoute registry, ported): provider/model lookup, `resolve()` (exact → basename → longest-prefix), capabilities, `glob_match()`, and the model exposure allow/deny predicate. Database-free, JSON loaded lazily |
| `routing.py` | The routing brain, ported from the OmniRoute domain/autoCombo layer: `tagRouter` (request `metadata.tags`), `policyEngine` (access/routing/budget, glob-matched), `accountFallback` + `lockoutPolicy` (`LockoutRegistry`, `ClientLockouts`), `fallbackPolicy` (per-model chains with priority + exclusion), combo strategies (priority/weighted/round-robin/random/least-used/cost-optimized), router strategies (rules/cost/latency/sla/lkgp), weighted `score_candidates`, and `build_plan()` which folds all of it into a dispatch plan plus an audit trail. Pure functions, no I/O |
| `router_settings.py` | The persisted half of the router: `RouterSettings` loaded from / saved to `SystemConfig` (strategy, weights, lockout tuning, policies, chains, exposure lists) + lockout flush/load |
| `kv.py` | SystemConfig KV helpers: `get_config`, `set_config`, `get_config_number`, `get_gpu_providers`, `set_gpu_providers` |
| `engine.py` | Sidecar lifecycle: `start_sidecar()` / `stop_sidecar()` / `health_check()`; spawns bun/node `engine/index.js`; degrades honestly if absent |
| `provider_transport.py` | Wire-format translators: `openai_to_anthropic`, `anthropic_to_openai`, `anthropic_url`, `anthropic_headers` (incl. tool calls/results) |
| `discovery.py` | Live model discovery: `discover_provider_models(provider)`, `aggregate_discovery()` for `?discover=1` |
| `syncengine.py` | Catalogue sync: `sync_provider(provider)` upserts models from upstream `/models` |
| `freemodels.py` | Loads `engine/free-models.json` (snapshot of ClawLabsAI/free-ai-models); feeds RouterX Free tiers `routerx/pro`, `routerx/air`, `routerx/mini` |
| `terminal/` | **The whole terminal feature, one path, self-contained** (imports nothing from `nova/`, needs no DB, so it deploys alone) — `pty.py`: real PTY session manager for Terminal tab sessions (`close_all()` on shutdown, OSC 7 cwd reporting, max 8 sessions, 30-min idle reap). `sandbox.py`: simulated allowlist executor (~1200 lines, no child processes, real OS telemetry, dangerous-token blocklist exit 126 / unknown exit 127) — the one module that borrows the app (history + `nova …`), degrading with an honest message when it isn't there. `link.py`: the seam (`LocalLink` in-process / `RemoteLink` over HTTP+SSE, `current()`/`pin()`/`is_remote()`). `api.py`: the HTTP contract mounted by BOTH hosts. `agentbox.py`: the AI agent served at `/agent/*` (tools run in visible PTY tabs) with a **custom provider registry** — `GET/POST/DELETE /agent/providers`, saved `0600` beside the workspace, keys only ever echoed masked, and the env-configured one (id `env`) kept as the default. No providers at all → honestly off (`agentbox_unconfigured`). `agentbox.html`: its page (shell tabs + chat + a providers panel). `service.py`: the standalone host (`python3 -m terminal.service`). `config.py`: every `NOVA_TERMINAL_*` / `NOVA_AGENTBOX_*` knob. `Dockerfile` + `requirements.txt`: build `./terminal` alone. `terminal/README.md` has the hosting notes |
| `agent.py` | Autonomous runner: plan (LLM strict JSON) → execute tool → persist AgentStep → repeat until `finish`/step limit → final summary. Tools: web_search, read_url, terminal, gateway_stats, storage_scan, discover_models, write_file, read_file, edit_file, mkdir, bash_exec, **mcp_call**. `agent_llm()` = engine sidecar first, this server's own `/v1/chat/completions` as fallback (`_default_model_id()` resolves `auto` to a healthy enabled model). Each DB touch opens its own SessionLocal; cancellation checked per loop. Async callable handed to FastAPI BackgroundTasks |
| `storage_lib.py` | Storage providers (Firebase/Supabase/B2/R2/GitHub…), backups, file ops |
| `mcp_client.py` | Transport-only MCP client: `list_tools` / `call_tool` / `probe`, streamable HTTP (JSON or SSE-framed, `Mcp-Session-Id` echo) + stdio plugin processes (argv-split, cached per server, capped at 8, `close_all()` on shutdown). Helpers: `parse_call` (`server::tool {json}`), `mask_headers` |
| `mcp_registry.py` | The user's plugins: CRUD over `McpServer` (validated, slugged, secrets preserved across a masked re-save), `refresh()` caches `tools/list` + status, `tool_catalogue()` feeds the agent prompt and `/api/agent/tools`, `invoke()` dispatches a call |
| `mcp_tools.py` | Ported from the OmniRoute MCP surface: the scope table (`scopes`/`tool_scopes`/`has_scopes`/`filter_by_scope`, least-privilege) and the ReDoS-safe lexical `search_tools()` used to pick which plugin tools to surface, plus the 46-entry curated agent-skill catalogue from `nova/data/agent_skills.json` |
| `gwlog.py` | `record_request()` — compact RequestLog writes from the gateway middleware |
| `synclog.py` | Catalogue-sync audit logging |

## routers/ — the HTTP surface

| File | Mounted at | Contains |
| --- | --- | --- |
| `gateway.py` | `/v1` + `/api/v1` | `GET /models` (+`?discover=1`), `POST /chat/completions` (SSE streaming + non-streaming, full fallback pipeline), `POST /completions` (legacy), `POST /messages` + `/messages/count_tokens` (Anthropic format), `POST /embeddings`. Pooled httpx clients; tunable timeouts `NOVA_CONNECT_TIMEOUT` (5s), `NOVA_UPSTREAM_READ_TIMEOUT` (600s — long thinking runs), `NOVA_NONSTREAM_READ_TIMEOUT` (300s); provider-key cache (30s TTL). 2xx bodies are validated; provider/engine faults become typed 502/503/504 with `x-request-id`, never a bare 500 |
| `anthropic_adapter.py` | (helper) | Native Anthropic Messages passthrough used by `/v1/messages` |
| `admin_providers.py` | `/api/admin/providers` | CRUD, presets, real connectivity probe (`/test`), signin/signout sessions |
| `admin_models.py` | `/api/admin/models` | List/filter, enable toggle, ping, catalogue sync |
| `admin_keys.py` | `/api/admin/keys` | Upstream key CRUD + bulk import + clear cooldowns |
| `admin_client_keys.py` | `/api/admin/client-keys` | Gateway client keys (`nova-sk-…`) |
| `admin_routes.py` | `/api/admin/routes` | Fallback chains CRUD + `/preview` (Stage 1/2/3 timeline) |
| `admin_terminal.py` | `/api/admin/terminal` | History/exec/clear + PTY session endpoints under `/pty/*` |
| `admin_compute.py` | `/api/admin/compute` | GPU/compute provider toggles + config |
| `admin_storage.py` | `/api/admin/storage` | Providers, connect/disconnect, files, backup |
| `admin_misc.py` | `/api/admin` | meta, stats, analytics, logs, and other misc |
| `admin_mcp.py` | `/api/admin/mcp` | Custom MCP servers: `/servers` (CRUD), `/servers/{delete,toggle,test,refresh,call}`, `/tools`. Header values are stored, never returned |
| `admin_routing.py` | `/api/admin` | Routing brain + catalogue surface: `/routing` (GET/POST settings), `/routing/availability[/clear]`, `/catalog` (+`/{key}`, `?q=` search), `/skills`, `/mcp/scopes` |
| `agent_api.py` | `/api/agent` | Task create/list/get/cancel + tools list |
| `_common.py` | (helpers) | `json_body`, `as_str`, `as_num`, `parse_int`, `epoch_ms`, `mask_key`, `not_found`, `invalid_id` |

## ui/ — the Python frontend

| File | Role |
| --- | --- |
| `shell.py` | `GET /` renders `base.html` (sidebar + empty tab container + footer); `/partials/footer-stats`; favicon |
| `tabs/__init__.py` | Tab registry: each module exports `router` with `GET /partials/tab/<key>`; register new tabs here |
| `tabs/*.py` | overview, console, providers, models, routes, keys, storage, analytics, logs, terminal. Optional `POST /ui/<key>/...` action endpoints returning HTML fragments |
| `api_client.py` | Async httpx client to the app's own JSON API (self-BFF); module-level `api` instance |
| `render.py` | Jinja2 `render()` / `html()` helpers (templates dir, HTMLResponse) |
| `format.py` | fmtNum / fmtBytes / timeAgo / maskKey style helpers ported from the TS original |

## Data flow examples

**Chat completion:** client → `POST /v1/chat/completions` → resolve ModelRoute (public_id match, else `*`) → Stage 1 direct upstream (enabled key) → Stage 2..N explicit fallback chain → final stage RouterX Free engine (`nova_engine` client → sidecar) → response `model` = requested id, `_nova` = truth (upstream_model, provider, stage, spoofed) → `RequestLog` row.

**Dashboard interaction:** browser HTMX → `GET /partials/tab/models` → `ui/tabs/models.py` renders `tabs/models.html` → fragment swapped into `#tab-content`; buttons POST to `/ui/models/...` → BFF calls `/api/admin/models/...` → re-render partial.

**Agent task:** `POST /api/agent/tasks` → BackgroundTasks → `nova/agent.py` loop → steps streamed/persisted → summary.

**Gateway request:** `POST /v1/chat/completions` → `route_request()` (`routers/gateway.py`) loads `RouterSettings` from `SystemConfig` → `nova.routing.build_plan()` (policy verdict → exposure lists → tag routing → lockouts → selection strategy) → ordered `Model` rows → each attempt feeds `note_attempt()` back into the `LockoutRegistry` → the `_nova.routing` block reports the strategy, the ordering and every dropped candidate.

## Extension points (where to add things)

- **New admin JSON endpoint** → `routers/admin_<domain>.py`; mount prefix in `main.py` (admin_router section). Reuse `_common.py` helpers.
- **New gateway endpoint** → `routers/gateway.py`; wire-format helpers in `nova/provider_transport.py`.
- **New dashboard tab** → `ui/tabs/<key>.py` (export `router`, `GET /partials/tab/<key>`), register in `ui/tabs/__init__.py`, template `templates/tabs/<key>.html`, partials in `templates/partials/`.
- **New DB entity** → class in `nova/models.py` (bootstrap creates missing tables additively); KV-only values can just use `nova/kv.py` SystemConfig helpers instead.
- **New free provider/model source** → `engine/free-models.json` + `nova/freemodels.py`.
- **New routing behaviour** → `nova/routing.py` (pure, unit-tested) plus the `SystemConfig` knobs in `nova/router_settings.py`; never inline a decision in `routers/gateway.py`. A new catalogue source goes in `nova/data/` behind a `nova/catalog.py` accessor.
- **New agent tool** → action in `nova/agent.py` `ALLOWED_ACTIONS` + executor branch + tools list in `routers/agent_api.py`.
- **New plugin/MCP server** → a row in `McpServer` (additive table) via `nova/mcp_registry.py`; nothing else needs touching — the console drawer and the agent catalogue read the same registry.
