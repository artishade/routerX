# RouterX — Feature Recipes (how to customize common things)

Fast paths for the most common customization requests. Each recipe lists the
exact files to touch, in order. Read `conventions.md` for the invariants behind
each step. After any recipe: `python3 -m compileall -q main.py nova routers ui`
then `sh scripts/test.sh`.

## 1. Add an admin JSON endpoint (e.g. new stats field or action)

1. Pick the domain file `routers/admin_<domain>.py` (or create one + mount it in
   `main.py` under `admin_router.include_router(...)` with a prefix).
2. Handler signature: `async def handler(request: Request, db: Session = Depends(get_db))`.
3. Parse body with `_common.json_body`, validate, return dict (snake_case) or
   `not_found()` / `JSONResponse({"error": ...}, 400)`.
4. If the UI needs it: add a method in `ui/api_client.py`, call it from a tab
   module, render a partial.
5. If a new config knob: store via `nova/kv.py` (SystemConfig) — no new column.

## 2. Add a new dashboard tab

1. Create `ui/tabs/<key>.py`:
   - `router = APIRouter(tags=["ui:<key>"])`
   - `@router.get("/partials/tab/<key>")` → fetch data via `api.get(...)` (BFF),
     render `tabs/<key>.html` with `html(render(...))`.
   - Optional action endpoints `POST /ui/<key>/<action>` returning HTML fragments
     (partials), never JSON-only responses.
2. Register in `ui/tabs/__init__.py` (import + add to the tuple loop).
3. Create `templates/tabs/<key>.html` (fragment, not full page) + partials in
   `templates/partials/` for action re-renders and error panels (see
   `partials/console_error.html` for the retry pattern).
4. Add the sidebar entry + TabKey-style wiring in `templates/base.html`.
5. Style per conventions #19–20 (darkest-red accent via the `emerald-*` remap, neutral `slate-*` surfaces, `green-*` success, no blue/indigo, aria-labels).

## 3. Add a provider preset / kind

1. Presets live in `routers/admin_providers.py` (preset catalogue table/constant)
   — add base_url, prefix, key_hint, free_tier, docs_url, auth_url, color, priority.
2. Kinds are validated against `PROVIDER_KINDS = {"openai", "anthropic", "gemini", "builtin"}`.
3. If it needs a new wire protocol: add translators in `nova/provider_transport.py`
   and a branch in `routers/gateway.py`'s pipeline + `admin_providers.py` test probe.
4. Keyless providers: add to `KEYLESS_PROVIDERS` set in `admin_providers.py`.
5. Update `/api/admin/meta` presets if the UI preset grid is driven from meta.

## 4. Add a model field / capability

1. Additive column in `nova/models.py` `Model` (nullable, with default) — bootstrap
   creates it on next boot; write no migration.
2. Surface in `GET /api/admin/models` (routers/admin_models.py) and gateway
   `/v1/models` entries as needed.
3. Capabilities (tools/vision/reasoning) are parsed JSON on the Model row — extend
   the parser, don't add columns, when possible.
4. UI: model cards in `ui/tabs/models.py` + `templates/partials/models_list.html`.

## 5. Add an agent tool

1. Add the action id to `ALLOWED_ACTIONS` in `nova/agent.py`.
2. Implement the executor branch in the tool-execution section of the loop
   (wrap in try/except — a failing tool records an error step and continues).
3. Add the tool descriptor to `routers/agent_api.py` tools list
   (id, name, description, icon).
4. UI picks it up automatically (console/agent tab renders the tools list).

## 6. Change gateway routing / fallback behavior

1. Pipeline lives in `routers/gateway.py` `chat_completions` — stages: direct →
   route chain (`ModelRoute` public_id match, else `*`) → RouterX Free engine.
2. Preserve invariants #14–17: spoofed `model` field, `_nova` metadata, honest
   502 from the engine, RequestLog on every branch (including errors).
3. Route-resolution helpers + `/api/admin/routes/preview` (Stage 1/2/3 timeline)
   live in `routers/admin_routes.py` — keep preview consistent with the pipeline.
4. Cooldowns/weights/key selection come from ProviderKey state +
   `nova/ratelimit.py` (`cooldown_seconds` honours `Retry-After`; defaults
   `NOVA_KEY_COOLDOWN_429`=60, `_5XX`=30, `_401`=300, `_MAX`=900). Per-(provider,
   model) usage caps are the `rate_limits` knob - `0` means unlimited.
5. Anthropic-format side: `routers/anthropic_adapter.py` + translators in
   `nova/provider_transport.py` (`openai_to_anthropic` / `anthropic_to_openai`).

## 7. Extend the sandbox terminal (command mode)

1. All in `terminal/sandbox.py` (simulated executor):
   - allowlist addition → policy section at the top;
   - realistic output → command implementation from OS telemetry (/proc,
     platform, shutil) + live DB state (models/kv) — no subprocess;
   - `nova <subcommand>` extensions follow the existing `nova status`/`nova gpu`
     pattern (read DB, print a table).
2. Remember: dangerous tokens → exit 126, unknown → exit 127, persist
   TerminalCommand (cap 200).
3. If it must run a REAL process, it belongs in PTY sessions
   (`terminal/pty.py`, reached through `terminal/link.py`) instead — but never
   bypass the allowlist in the command executor.

## 8. Storage provider integration

1. Provider logic in `nova/storage_lib.py`; rows in `StorageProviderRow`/`StorageFile`.
2. Endpoints in `routers/admin_storage.py` (connect/disconnect/test/files/backup).
3. UI in `ui/tabs/storage.py` + `templates/tabs/storage.html` (connect dialogs in
   `templates/partials/storage_connect.html`).
4. Backup = snapshot JSON to the active provider; secrets masked with `mask_key()`.

## 9. Engine sidecar changes (free models)

1. Sidecar code: `engine/index.js` (zero npm deps — plain fetch/routing only).
2. Catalogue: `engine/free-models.json` (RouterX's own free-model list).
   Refresh via `POST /api/admin/models/sync`; the keyless providers in
   `nova/data/provider_registry.json` feed it through `nova/registry.py`
   `registry_models_for_engine()`.
3. Tier aliases (`routerx/pro`, `routerx/air`, `routerx/mini`) resolve in `nova/freemodels.py`.
4. Sidecar lifecycle/port: `nova/engine.py` + `NOVA_ENGINE_PORT` in `nova/config.py`
   (never the app's port). If the sidecar can't run, the gateway degrades to
   configured upstreams — keep that honest.

## 10. Config knob end-to-end

1. Env var → `nova/config.py` constant.
2. Optional dashboard exposure → `admin_misc.py` meta descriptor (MetaConfig).
3. Persisted-at-runtime knobs → `nova/kv.py` (SystemConfig KV) with
   `get_config_number` etc.
4. UI control → the owning tab module + partial, styled per conventions.

## 11. Add a read-only exchange adapter or trading read

1. Adapter: `nova/trading.py` -- add the exchange branch to `SUPPORTED_EXCHANGES` /
   `resolve_exchange` and implement normalized `read_quote` / `read_positions` /
   `read_balances` calls. Raise `TradingError(status, code, message)`; never retry
   or call a second exchange.
2. Gate: every read calls `ensure_trading_enabled(db)` before exchange I/O;
   missing credentials -> `exchange_credentials_missing`. Unknown exchange ->
   `unsupported_exchange` (501); a venue with no position endpoint ->
   `positions_unsupported` (501).
3. HTTP: `routers/trading.py` -- add a GET route behind `gateway_auth`, enforce
   the matching `trading:*` scope, and resolve one configured `exchange` Provider.
   Both `/v1` and `/api/v1` prefixes come from `router.include_router(trading_router)`
   at the bottom of `routers/gateway.py`.
4. Agent (optional): `nova/agent.py` `ALLOWED_ACTIONS` + executor + prompt line +
   `routers/agent_api.py` `TOOLS`; call `nova.trading.read_*` with `actor="agent"`
   and surface `trading_disabled` as a refusal.
5. Tests: `tests/test_trading_service.py` (adapter; swap the module-level
   `TRANSPORT = httpx.MockTransport`) and `tests/test_trading_gateway.py` (auth,
   scopes, kill-switch, typed errors). No network.

```bash
python -m unittest tests.test_trading_service tests.test_trading_gateway -v
python3 -m compileall -q main.py nova routers ui terminal
```

## Verification checklist (after any recipe)

```bash
python3 -m compileall -q main.py nova routers ui   # fast syntax gate
sh scripts/test.sh                                  # full offline suite
# manual, if relevant:
python3 main.py &                                   # boots on :3000
curl -s localhost:3000/health | head -c 200
curl -s localhost:3000/api/admin/meta | head -c 200
```
