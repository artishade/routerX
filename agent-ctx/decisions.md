# RouterX — Decision Log (why things are this way)

Intent-preservation memory. If a change would contradict an entry here, check
with the user before overriding, or add a new dated entry explaining the pivot.

## 2026-10-28 — The catalogue is curated, and every base_url is an API root
- **A big registry was a liability, not a feature.** The previous catalogue was
  a ~30k-line generated dump: a few hundred providers and thousands of models,
  most of them dead proxies, retired replicas or endpoints that never answered.
  Every dead entry is a request that burns a fallback stage before a real
  provider gets a chance, and a list where most rows fail cannot honestly be
  called a "free tier". It is now a small, hand-maintained list
  (`nova/data/provider_registry.json`, 19 providers / 86 models) where every
  entry is one that actually responds. Fewer rows, all of which work, is the
  only catalogue worth shipping.
- **`base_url` is always the API root.** The generator stored some values as
  full endpoints (`…/chat/completions`, `…/models`). The gateway appends the
  path itself, so such a provider requested `…/chat/completions/chat/completions`
  and could never answer — silently, because discovery still looked fine.
  `nova/registry.py` `validate()` now rejects an endpoint-shaped `base_url`,
  and every consumer (gateway, discovery, probes) relies on appending.
- **One flag for keyless.** `requires_key: false` *is* the keyless marker;
  there is deliberately no separate `keyless` field. A keyless provider must
  also declare `auth_type: none`, which `validate()` enforces — no credential
  can be "required" and "not required" at the same time.
- **The catalogue is validated as data, not trusted as code.** `summary()`
  feeds the dashboard counts, `is_keyless()` the free surface, and
  `registry_models_for_engine()` the sidecar's model list, so the two numbers a
  user sees can never disagree. Regenerating the registry is a deliberate,
  reviewed data step.

## 2026-10-28 — Gateway auth: fail-closed once a client key exists, fail-open on a fresh install
- **A fresh install must not demand a credential it never printed.** Someone
  who runs `docker compose up` and calls `/v1/chat/completions` has no client
  key to authenticate with; refusing them would make the first-run experience
  a 401 with no way forward. So with zero enabled `ClientKey` rows the gateway
  stays anonymous-friendly.
- **The first enabled key flips the rule, immediately.** A client key exists
  to be enforced; if a deployment can mint a key and still accept anonymous
  calls, revocation is a fiction. The moment one enabled key exists, a request
  with no credential is `401 missing_api_key`. `NOVA_REQUIRE_API_KEY` forces the
  same for deployments that want keys from day one.
- **Unknown and disabled tokens are always refused.** Treating a revoked token
  as "anonymous" would let a revoked key keep working on a fail-open
  deployment — the exact inverse of revocation. It is `401 invalid_api_key`, and
  repeated failures are throttled in memory.
- **Usage is written in exactly one place.** `nova/auth.py` `attribute()` /
  `update_usage()` is the single writer of `ClientKey` counters (`reqCount`,
  `lastUsedAt`, tokens), so no endpoint has to remember to book usage. The
  model allow-list, the sliding-minute RPM and the daily token budget are
  checked once the requested model is known.

## 2026-10-28 — Upstream rate limits default to unlimited
- **Silently throttling a working deployment is worse than a provider 429.**
  A gateway's job is to move a request to a provider that can serve it. If
  RouterX invented per-minute or per-day caps on first upgrade, a working
  deployment would start refusing traffic it used to serve, and the cause would
  be our default, not the operator's configuration.
- **So every limit is opt-in, and `0` means no limit.** `NOVA_DEFAULT_RPM/TPM/
  RPD/TPD` default to `0`, the `rate_limits` SystemConfig knob starts empty, and
  `capped()` short-circuits to an empty set when nothing is configured — the
  candidate pool is exactly what it was before the module existed. A knob that
  sets only `rpm` caps the minute and does not invent a token budget.
- **What the module *is* for:** when an operator does set a cap, a request that
  is already out of budget is dropped *before* ordering, so the next candidate
  serves it instead of a round trip being wasted on a guaranteed 429. Key
  cooldowns are separate and always on: a key that got a 429/401/5xx sleeps,
  honouring the provider's `Retry-After`.

## 2026-10-28 — The response cache is opt-in and never touches tool calls or streams
- **A cache changes behaviour, so it cannot be a default.** Storing and
  replaying completions is a semantic change (a "fresh" call returns an older
  answer); on an upgrade it must be invisible. `NOVA_RESPONSE_CACHE` starts off
  and the `response_cache` knob starts empty.
- **Only a plain 200 text completion is worth storing.** Streaming has no body
  to key on and tool calls are the agent loop's control flow — replaying a
  cached tool call would make the loop act twice on a stale decision. So
  `cacheable()` rejects both, and `x-nova-no-cache: 1` bypasses read and write
  for callers that must never get a cached answer.
- **The key is the request, not the moment.** Temperature, `max_tokens`, `top_p`
  and the tools are part of the address; user ids and headers are not, because
  they do not change the text a provider generates. Anything that is not in the
  key is deliberately considered "same answer".

## 2026-10-28 — `auto:<profile>` resolves before routing; an unknown profile is a 404
- **A profile is a routing instruction, not a model.** `auto:fast` must change
  *how* the request is routed (a fallback list and an optional strategy), never
  what the response claims to be: the response `model` stays `auto:fast`, and
  the profile is applied as a plan override before the candidate pool is
  ordered.
- **An unknown profile is an error, not an opportunity to guess.** Falling
  back to plain `auto` would silently bill or route against a different chain
  than the caller named — a "cheap" client would quietly get an expensive
  model and never know. It is a 404 that says the profile does not exist.
- **Profiles are plain config.** They live in the `routing_profiles`
  SystemConfig key and are validated by `nova/profiles.py` `normalize_spec()`,
  so an unknown strategy is dropped at write time rather than turning a request
  into a runtime surprise.

## 2026-10-28 — A trade stage is terminal: trading breaks the fallback invariant on purpose
- **The fallback chain assumes a call is safe to repeat.** Text generation is:
  a second provider returning the same content costs nothing but a retry.
  Placing an order is not. If the first exchange's response is lost on the way
  back, the order may still have filled — re-sending it to a second provider
  can double the position. Money is not idempotent by default.
- **Therefore the trading roadmap opts out explicitly.** (Phase 3 onward — not
  implemented yet.) A trading request is resolved by a terminal stage: a
  failure is a typed decision (`insufficient_funds`, `market_closed`,
  `risk_rejected`, …), and the router must never dispatch it to the next
  candidate. This is the one place where "the client keeps asking for one model
  id and still gets an answer" is the wrong outcome — the honest answer is
  "we do not know whether it filled".
- **Idempotency keys and a kill-switch are preconditions, not features.** Any
  retry inside a trading flow has to be idempotent at the exchange, and the
  Phase 0 kill-switch must be able to refuse every trade tool before any live
  execution code ships.

## Platform pivots

- **2026-09 · Python port (current).** The whole app is FastAPI + Jinja2 + HTMX.
  The previous TypeScript implementation (and an intermediate Next.js 16 rebuild
  documented in `worklog.md` / `agent-ctx/api-contract.md`) is history — kept
  only as the API-shape reference. Reason: single runtime, no Node build step,
  one process serving UI + API.
- **UI = self-BFF, server-rendered.** `ui/` calls its own process's JSON API
  over HTTP instead of querying the DB directly. Reason: UI behaviour can never
  drift from gateway behaviour; tab modules stay thin; fragments (not JSON)
  drive HTMX updates.
- **Gateway parity with the TS original** (pipeline stages, spoofing, `_nova`
  metadata, RequestLog) was an explicit porting requirement — treat divergences
  as bugs, not improvements.

## Tricky tradeoffs worth remembering

- **Simulated sandbox executor** (`terminal/sandbox.py`): no child processes by
  design; outputs are synthesized from real OS telemetry + DB state. The only
  allowed subprocess is a `--version` probe of node/bun/npm. Interactive work
  goes through real PTYs (`terminal/pty.py`) instead. Don't "fix" the
  executor by spawning shells — that's the vulnerability it exists to avoid.
- **Upstream read timeout is 600s** (`NOVA_UPSTREAM_READ_TIMEOUT`): long
  thinking runs legally sit silent for minutes; a 60s timeout caused
  mid-stream ReadTimeout failures in Claude Code. Tunable via env, default high.
- **Pooled httpx clients** (`UPSTREAM_CLIENT` / `NONSTREAM_CLIENT` in
  `routers/gateway.py`): fresh clients per request pay TCP+TLS per call; the
  pool keeps warm connections. Reuse them; don't instantiate per request.
- **Provider-key cache (30s TTL)** in `gateway.py`: avoids a DB roundtrip per
  upstream attempt. Acceptable staleness by design.
- **Identity spoofing**: the response `model` is always the requested id; the
  real upstream is in `_nova`. Clients (incl. Claude Code) depend on this.
- **Engine sidecar port** never shares the app's HTTP port (Freebuff preview
  proxy points at the single app port; a second listener there makes the proxy
  answer with the sidecar's 404). Default 3099, steps aside on collision
  (`ENGINE_SIDECAR_PORT` in `nova/config.py`).
- **Sandbox preview sets `NOVA_ENGINE_DISABLED=1`** so the preview URL resolves
  to the dashboard, not the sidecar. Production (Docker/Render) unaffected.
- **Additive-only bootstrap**: schema sync creates missing tables; demo-data
  seeds from legacy deployments are cleaned, never created. Reason: deploys must
  never wipe user data (SQLite-in-container is ephemeral; Postgres recommended).
- **Ephemeral-DB warning** is intentional UX (banner + `/health` storage block):
  it tells users to set a Postgres `DATABASE_URL` before their data vanishes.
- **Prisma-style DATABASE_URL normalization** (`file:…`, `postgres://`,
  pgbouncer params, explicit psycopg2/psycopg3 dialect pinning) exists so the
  same connection string works across local/Docker/Render/SQLAlchemy versions.
- **Both `/v1/*` and `/api/v1/*`** mount the same gateway router: OpenAI-SDK
  clients expect `/v1`, some internal tooling uses `/api/v1`. Keep both.
- **Tab registry pattern** (`ui/tabs/__init__.py` loops over modules exporting
  `router`): adding a tab must not require editing `main.py`.

## Keep memory accurate

- When a decision is reversed or a big feature lands, append a dated entry here
  and update `AGENTS.md`/`project-map.md` — that's the whole job of the
  `nova-memory-sync` skill.

## 2026-10-02 — The terminal is one path, and hostable on its own
- **Everything terminal lives under `terminal/`.** `pty.py` (real shells),
  `sandbox.py` (allowlist exec), `link.py` (the seam), `api.py` (the HTTP
  contract), `service.py` (the standalone host), `config.py`, `run.sh`. Reason:
  the interactive terminal hands out root shells, so it has to be sizeable,
  restartable and network-isolable on its own — that is only possible if the
  feature is one directory instead of four spread across `nova/` and `routers/`.
- **The app imports the terminal by path, never the reverse.** `main.py`,
  `routers/admin_terminal.py` and `nova/agent.py` all do `from terminal import
  link` / `from terminal.api import router` / `from terminal.sandbox import …`.
  `terminal/` may use `nova`'s pure config/model/telemetry helpers, never the
  other way round; `terminal/__init__.py` re-exports the seam but deliberately
  not `api` (FastAPI) or `sandbox` (SQLAlchemy), so importing it stays cheap.
- **One contract, two hosts.** `terminal/api.py` is mounted by the gateway
  (`/api/admin/terminal/pty/*`) *and* by the service (`/terminal/pty/*`), so a
  separately hosted terminal can't drift from the local one. `link.py` picks
  `LocalLink` or `RemoteLink` from `NOVA_TERMINAL_URL` alone.
- **Errors carry a stable `code`** (`session_gone`, `session_limit`,
  `link_unavailable`, …) so a remote failure reads the same as a local one.
  `routers/admin_terminal.py` keeps only the dashboard glue (DB history, OS
  telemetry) and mounts the terminal's router at `/pty`.
- **Terminal settings live in `terminal/config.py`, not `nova/config.py`** — a
  host that deploys only `terminal/` still has to read `NOVA_TERMINAL_*`.
- **`terminal/` imports nothing from `nova/`, all the way down.** It reads the
  environment itself, so it is a genuinely separate deployable: `docker build
  ./terminal` produces an image with three dependencies and no database, no
  gateway, no UI. `sandbox.py` is the single exception — it borrows the app for
  command history and `nova …`, so it imports those names optionally and reports
  `APP_AVAILABLE` at every entry point rather than crashing a terminal that has
  no gateway behind it.
- **Agentbox ships inside the terminal, not beside it.** A root prompt without a
  mind behind it is a half-product, and the whole reason people split the
  terminal off is that it has different hardware and a different lifecycle. So
  `/agent/*` (chat, models, and a page showing shells and chat together) is
  mounted by the terminal host only — the gateway already serves `/api/agent/*`,
  and two agents competing for the same routes would be worse than one of them
  being in the right place. Its tools run through the link, so commands land in
  real PTY tabs on whichever host is answering. It is off unless
  `NOVA_AGENTBOX_BASE_URL` is set: no hidden outbound calls, no silent
  "configured" claims.
- **The host serves its own page at `/`.** A terminal deployed on its own once
  answered `{"detail":"Not Found"}` on its homepage: every API route existed
  and no page did, which makes the whole feature useless in a browser.
  `terminal/web/` is that page — session tabs, a real xterm.js shell, Agentbox
  beside it — served by `terminal/service.py` at `/` (and `/agent`, where the
  docs send people). The client keeps a plain append-only fallback because a
  terminal that renders nothing the moment jsdelivr is blocked is not a
  terminal. A hosted terminal therefore needs no dashboard, no gateway and no
  build step to be usable.
- **Providers are a registry, not a single env var.** One endpoint baked in at
  deploy time (`NOVA_AGENTBOX_BASE_URL`, id `env`) is not a config the person
  sitting at the terminal can change, and "which model" is exactly the thing
  they want to switch. So `GET/POST/DELETE /agent/providers` manages a small
  JSON registry (mode `0600`) beside the workspace, the page has a panel for
  it, and each `chat` names a provider. Three rules keep it safe: an API key is
  only ever echoed back masked, an edit with a blank key keeps the stored one,
  and `env` cannot be deleted over the API (unset the variables instead). A
  provider that does not exist is a 404 that lists the ones that do — never a
  silent fall back to the default, which would quietly bill the wrong account.

## 2026-10-02 — The routing brain is RouterX's own, as pure functions
- **The routing model is a decision layer, not a codebase.** RouterX stays
  Python; `nova/routing.py` implements `tagRouter`, `policyEngine`,
  `accountFallback`/`lockoutPolicy`, `fallbackPolicy`, the combo strategies and
  `routerStrategy` as pure functions over plain dicts, and `nova/catalog.py`
  reads the curated `nova/data/provider_registry.json`. Reason: an executor-model
  codebase does not port — but the *decisions* do, and the decisions are the
  valuable part.
- **Decisions live in `nova/`, never in `routers/gateway.py`.** The gateway only
  supplies rows and records outcomes. Everything a routing change touches is a
  pure function with a unit test in `tests/test_routing.py`.
- **The catalogue is data, not code.** `nova/data/provider_registry.json` is a
  curated, hand-maintained document; dead and retired entries are simply not in
  it. Changing it is a reviewed data step, not a code change. *(Superseded
  2026-10-28: it is now a small curated list, not a generated snapshot.)*
- **Seeding is additive and providers start disabled.** `bootstrap` inserts only
  rows that are missing and never touches an existing one, so a provider the
  operator already configured keeps its own base URL, prefix and priority, and a
  model they disabled stays disabled. The registry providers ship `enabled=false`
  because they have no key yet; the gateway already skips a provider with no
  enabled key, so seeding cannot cause traffic to move. `NOVA_SEED_REGISTRY=off`
  or `SystemConfig seed_registry_models=off` skips it entirely.
- **One predicate, two chokepoints.** The exposure allow/deny list gates
  `/v1/models` *and* candidate selection, so a hidden model can never re-enter
  through a fallback chain. That is exactly why the exposure list is one shared
  predicate rather than two filters.
- **Scopes are opt-in.** `has_scopes()` treats "no scopes declared" as permitted,
  because the console and the agent run in-process with no scope table and
  locking them out would be a regression. A credential that *does* declare scopes
  gets them enforced.
- **The tab lists are paged.** The Providers and Models tabs render a bounded
  first page and say "N of M"; search narrows to the rest, so the UI stays fast
  however large the catalogue grows. *(The catalogue itself is now curated —
  see the 2026-10-28 entry.)*

## 2026-10-01 — Custom MCP servers are the console's plugin system
- **One table (`McpServer`), two transports.** A plugin is either a streamable
  HTTP MCP endpoint or a local stdio process. Legacy HTTP+SSE (GET stream +
  POST side channel) is deliberately not implemented: every current server
  speaks streamable HTTP, and a half-working second transport is worse than an
  honest two. Reason: plugins must not turn the gateway into a transport zoo.
- **Discovery is cached on the row, never probed at prompt time.** The agent
  prompt is built from the `tools/list` snapshot, so a slow or dead plugin can
  never stall a task — it simply is not offered. "Test"/"Refresh" re-probe on
  demand. Reason: prompt building is on the task's critical path.
- **stdio children are cached (max 8), argv-split, `shell=False`.** A plugin
  boots in ~350 ms; reusing the child keeps repeated agent steps cheap. Nothing
  runs through a shell — `shlex.split` + `create_subprocess_exec` only.
  `mcp_client.close_all()` runs on shutdown, next to `terminal.pty`'s
  `close_all()`.
- **Header values are credentials.** They are stored, masked on every read, and
  a re-save whose value is still masked keeps the stored one — the browser
  never holds the real token, so rotating it means typing a new one.
- **One registry, two consumers.** `nova/mcp_registry.py` backs both the console
  drawer (`/ui/console/plugins*`) and the agent (`mcp_call` action), so what
  the user sees in the UI is exactly what the planner can call.

## 2026-10-28 — Gateway failures are typed, never bare 500s
- **A 2xx from an upstream is not proof of JSON.** `attempt_upstream`,
  `_try_native_anthropic` and the engine client now parse through
  `response_json_dict()` / an explicit check. A degraded provider answering
  HTML/empty/array used to raise `ValueError` out of the endpoint — the exact
  "discovery works, inference returns 500" shape. It is now a failed stage, so
  the fallback chain continues.
- **The native Anthropic passthrough uses the same plan as everything else.**
  It used to call `find_model()` directly, bypassing the policy engine,
  exposure lists, and stateful ordering. `_try_native_anthropic(..., rows=…)`
  and `pipeline_nonstream(..., precomputed=…)` now share one `route_request()`
  decision per request, so the passthrough cannot dispatch a model the router
  excluded.
- **Unexpected exceptions are translated at the boundary.** Non-stream
  `/v1/messages` and `/v1/chat/completions` return 502/503 in the client's
  protocol shape, streaming paths emit an SSE `error` event, and `main.py` has
  a last-resort exception handler. Every gateway response carries
  `x-request-id`; the same id is in the error message and server log.
- **Schema drift is a 500 factory.** `create_all` creates missing tables but
  never ALTERs an existing one, so an upgraded DB could keep an old
  `ModelRoute` without `.auto` while `/v1/models` still worked and every
  inference query failed. `bootstrap.sync_schema_columns()` adds missing
  columns with constant defaults (never NOT NULL, never rewriting rows) before
  seeding. Additive, per the data-safety contract.
- **SQLite self-hosting gets WAL + a 30s busy timeout.** Concurrent agent
  turns used to turn ordinary write contention into `database is locked`
  (OperationalError) and then 500. Postgres remains the recommended store.
- **Agent-loop conversion fixes:** Anthropic `tool_result.tool_use_id` is now
  read (not the OpenAI spelling), tool results precede user text, and
  `openai_to_anthropic` merges parallel tool results into one user turn —
  without that, the second Claude turn was rejected by Anthropic.
