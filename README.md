# RouterX

**One endpoint in front of every AI provider — with automatic failover that actually uses the providers you configured.**

RouterX is a self-hosted, OpenAI- and Anthropic-compatible AI gateway. Point your SDKs (or **Claude Code**) at one base URL and RouterX fans every request out across the providers you've added, walks a fallback chain when one fails, keeps a per-provider key pool with cooldowns, and finally falls back to **RouterX Free** — a zero-key free-model engine so the gateway never leaves a request unanswered. It ships with an 11-tab dashboard, a chat console, and a real cloud terminal with an autonomous agent.

![stack](https://img.shields.io/badge/Python-FastAPI-009688) ![stack](https://img.shields.io/badge/SQLAlchemy-2-orange) ![stack](https://img.shields.io/badge/UI-Jinja2%20%2B%20HTMX-dc2626) ![stack](https://img.shields.io/badge/Claude%20Code-ready-8b5cf6) ![license](https://img.shields.io/badge/license-MIT-blue)

**100% Python** — the backend and the dashboard are one FastAPI process (Jinja2 + HTMX + Alpine, no Node build step). The only JavaScript in the project is the `static/app.js` wiring the dashboard, the terminal client, and the tiny zero-dependency free-model engine sidecar.

---

## Why RouterX?

| Problem | What RouterX does about it |
| --- | --- |
| 🔑 **Provider sprawl** | Add OpenRouter, Groq, Anthropic, xAI, a Cloudflare Worker, or any OpenAI-compatible gateway once. One base URL serves every model, every SDK, no code changes. |
| 💥 **A provider goes down mid-task** | Requests fail over through your explicit chain **and then across every other healthy provider** before ever giving up — the client keeps asking for one model id and still gets an answer. |
| 🧊 **Freezes / "request failed" on long runs** | Long-thinking models go quiet for minutes. RouterX keeps the stream alive with heartbeats, so Claude Code and streaming clients don't die at the 10–15s mark. |
| 🧩 **Nobody can find the right model** | Every model in the curated catalogue is searchable right where you build a fallback route — type `claude`, `gpt-4`, `qwen` and click. |
| 🧠 **You don't want to babysit keys** | Per-provider key pools, weights, cooldowns that honour `Retry-After`, and a (provider, model) lockout registry that backs off failures and clears itself the instant a provider recovers. |
| 🆓 **Free models, honestly** | A built-in **RouterX Free** engine serves free models with **no API key at all** — every keyless provider in the catalogue works out of the box — and tells you the truth via `_nova` when it's used. |
| 🖥️ **You want it on your own box** | One `docker compose up`. SQLite by default, Postgres when you want persistence, optional separate terminal host. |

### The catalogue is curated, not scraped

RouterX ships `nova/data/provider_registry.json`: a small, hand-maintained list (**19 providers, 86 models**) where **every entry is one that actually responds**. Four providers are keyless (`requires_key: false`) and work with zero credentials; the other fifteen are well-known hosted APIs (OpenAI, Anthropic, Gemini, OpenRouter, Groq, and so on), several of which offer a free tier, and need a key you supply. By wire format: 17 OpenAI-compatible, 1 Anthropic, 1 Gemini. Every `base_url` is an API **root** — the gateway appends `/chat/completions`, `/models` or `/v1/messages` itself — and `nova/registry.py` validates that on demand (`validate()`, `summary()`, `is_keyless()`, `registry_models_for_engine()`).

A big catalogue is not a better catalogue. Every dead endpoint in a list is a request that silently costs a fallback stage, and every wrong endpoint is a provider that can never answer. A short list you can trust is what makes the free tier honest. Operators can still add any OpenAI-compatible provider from the dashboard.

---

## See it work

**Auto-fallback: a failed provider rolls onto the next healthy one, then the free engine.** The client never sees the failure.

![RouterX auto-fallback demo](docs/fallback-demo.svg)

> If GitHub doesn't animate SVG, the static frames still show the flow: request → direct (429) → auto stand-in (200) → response keeps the requested model id.

### Screens

| Dashboard | Fallback-route builder (with model search) | Console |
| --- | --- | --- |
| ![Overview](docs/screenshots/overview.svg) | ![Routes](docs/screenshots/routes.svg) | ![Console](docs/screenshots/console.svg) |

*(These are vector previews of the dashboard's dark-terminal UI; run it locally to see the live HTMX version.)*

---

## Architecture

![RouterX architecture](docs/architecture.svg)

```mermaid
flowchart TD
    C["Clients<br/>OpenAI SDK · Claude Code · curl · dashboard"] --> A["Gateway auth — nova/auth.py<br/>Bearer / x-api-key · client-key limits"]
    A --> G["RouterX Gateway (FastAPI)<br/>/v1/* &amp; /api/v1/* · SSE streaming"]
    G --> VP["Virtual model ids — nova/profiles.py<br/>auto · auto:&lt;profile&gt;"]
    VP --> R["Routing brain — nova/routing.py<br/>policies → exposure → tags → lockouts → strategy"]
    R --> F{"Fallback chain"}
    F -->|1 direct| P1["Provider transport (pooled httpx)<br/>OpenAI-compatible · Anthropic · Worker · custom"]
    F -->|2 explicit chain| P1
    F -->|3 auto stand-ins| P2["Every other healthy, enabled provider"]
    F -->|4 last resort| E["RouterX Free engine (JS sidecar)<br/>curated keyless models, no key"]
    G --> DB[("SQLAlchemy 2<br/>Providers · Models · Keys · Routes · RequestLog · UsageWindow")]
    G -. optional .-> T["Terminal (hostable alone)<br/>PTY shells · sandbox · Agentbox agent"]
```

A request is ordered by a pure function (`nova/routing.py`) and then executed as a fallback chain: **direct model → your explicit route chain → auto stand-ins → the free engine**. Every stage is recorded, so `_nova.routing` can always explain *why* a request was answered by the provider it was.

---

## ✨ Features

| Area | What you get |
| --- | --- |
| 🔀 **Real cross-provider failover** | Direct → explicit chain → **auto stand-ins across every other healthy, enabled provider** → free engine. Opt out per route with the `auto` switch. |
| 💬 **Claude Code &amp; Anthropic-native** | `/v1/messages` + `/v1/messages/count_tokens`, full streaming event order, tool_use/tool_result passthrough, and raw passthrough to Anthropic-kind providers. |
| 🔌 **OpenAI-spec API** | `/v1/models`, `/v1/chat/completions` (SSE + non-streaming), `/v1/completions`, `/v1/responses`, `/v1/messages` + `/v1/messages/count_tokens`, `/v1/embeddings`, live discovery. Every endpoint also exists under `/api/v1/*`. |
| 🛡️ **Streams that survive** | Heartbeats while an upstream is silent + 600s read tolerance, so long prefill/thinking runs never trip a proxy timeout mid-answer. |
| 🧭 **Routing brain** | Policy engine, glob exposure lists, `metadata.tags` steering, per-(provider, model) cooldowns, and 11 selection strategies (priority, weighted, round-robin, least-used, cost/latency/SLA, last-known-good…). |
| 🎯 **Named routing profiles** | `auto` is your default chain; `auto:fast`, `auto:cheap` … are named profiles (fallback list + strategy) resolved before routing. An unknown profile is a 404, never a silent fallback. |
| 🔐 **Gateway client keys** | `Authorization: Bearer` / `x-api-key`, per-key model allow-lists, RPM limits and daily token budgets. Once one enabled key exists, anonymous requests are refused. |
| 🧮 **Usage windows &amp; key cooldowns** | Per-(provider, model, key) minute/day counters (`UsageWindow`) gate the candidate pool, and upstream keys cool down on 429/401/5xx honouring `Retry-After`. |
| 🧵 **Sticky sessions** | Send `x-nova-session` and the conversation stays on the model that answered the first turn — a preference, never a constraint. |
| ⚡ **Opt-in response cache** | Content-addressed TTL + LRU cache for non-streaming completions. Off by default; never caches streaming or tool-call responses. |
| 🔎 **Searchable route builder** | Build a fallback chain by searching the whole catalogue (`/ui/routes/models`) and clicking models in order — plus a 3-stage resolution **preview** that matches runtime exactly. |
| 🧠 **Free models, no key** | Built-in **RouterX Free** engine + convenience tiers (`routerx/pro`, `routerx/air`, `routerx/mini`). |
| **Trading foundation (read-only)** | `exchange` providers add a separate trading namespace: normalized quotes, positions and balances under `/v1/trading/*` (also `/api/v1/*`), per-client-key trading scopes, a global kill-switch and an append-only audit log. Read-only adapters today: Binance and Coinbase Exchange. **No order execution.** |
| 🔌 **MCP plugins** | Register your own MCP servers (streamable HTTP or local stdio); their tools join the agent's toolbox and a built-in "run this tool" form. |
| 🗂️ **Curated catalogue** | 19 providers / 86 models, 4 of them keyless, seeded on first boot and extendable with live discovery. |
| 🔑 **Key management** | Multi-key pools per provider, bulk import, weights, cooldown tracking, client keys for the gateway. |
| 🖥️ **Console + terminal** | One chatbox (`$ cmd` shell, `! task` agent) and a real xterm.js cloud terminal with an optional standalone deployment. |
| 🗄️ **Storage / compute** | Storage manager (R2, S3, GCS, B2, Azure, Supabase…) and compute config (GPU pool, gateway RAM booster). |
| 🎨 **UI/UX** | Matte-black terminal aesthetic on `#050505` with a darkest-red interaction accent (green = healthy, amber = warning, rose = error, purple reserved for RAM/spoof accents), plus a "Dark AI World" layer (`static/nova-world.css`) adding an animated aurora backdrop, reveal-on-scroll and hover glow — all disabled under `prefers-reduced-motion`. Server-rendered Jinja2 + HTMX, responsive mobile-first. |

---

## 🚀 Quickstart

### Docker (recommended)

```bash
git clone https://github.com/artishade/Novarouter.git
cd Novarouter
docker compose up -d
```

Open **http://localhost:3000** (or `http://localhost:$PORT`). On first start RouterX creates the schema and bootstraps the real minimum: the built-in free engine + the curated provider catalogue + gateway config. **No mock data is ever seeded.** Data lives in the `nova-db` volume.

```bash
docker build -t routerx .
docker run -d -p 3000:3000 -v nova-db:/app/db routerx
```

### Local (Python)

```bash
git clone https://github.com/artishade/Novarouter.git
cd Novarouter
pip install -r requirements.txt
cp .env.example .env
python3 main.py             # http://localhost:3000 — UI + API in one process
```

`python3 main.py` binds `0.0.0.0:$PORT` (`PORT` defaults to 3000). It serves **everything**: the gateway, the admin/agent JSON APIs, and the dashboard.

### Deploy (Render or any Docker host)

1. **New → Web Service** → connect the repo → **Runtime: Docker**.
2. Set **Health Check Path** to `/health`.
3. Set `DATABASE_URL` to a free Postgres URL (Neon/Supabase/Aiven) — see below.

> **Render note:** Render puts every service behind its own Cloudflare WAF, which
> you cannot configure. That WAF sometimes blocks *legitimate* agent traffic
> whose JSON body contains shell-injection-looking text (for example a tool call
> that pipes into an interpreter, `... | python -c "..."`). Those requests are
> answered with a Render HTML `403 - Forbidden` page **before RouterX is
> reached**, so the gateway cannot log, route or fix them. If you use Claude
> Code/Desktop as a client, prefer a host where you control the proxy/WAF, or
> run RouterX locally / behind an SSH tunnel. See the Claude troubleshooting
> note below.

### 💾 Keep your data across deploys (important)

Without `DATABASE_URL`, RouterX uses SQLite inside the container, so a redeploy on an ephemeral host resets providers, keys and routes. Point `DATABASE_URL` at a free Postgres instance instead:

```bash
DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require
```

Prisma-style `postgres://…` values and `pgbouncer`/`connection_limit` params are normalized automatically. Bootstrapping is **strictly additive** — it creates missing tables, adds missing columns, and seeds only what's missing, and never deletes, resets or purges anything. The dashboard shows a warning banner and `/health` reports `"storage": {"persistent": false}` until this is set.

### Scripts

The app is Python; the `package.json` scripts are thin `sh`/`python3` wrappers, so `bun run <script>` and `npm run <script>` behave identically.

| Command | What it does |
| --- | --- |
| `bun run install:py` | `pip install -r requirements.txt` |
| `bun run preview` | `scripts/preview.sh` — install deps if needed, then run on `0.0.0.0:$PORT` |
| `bun run dev` | `python3 main.py`, teeing output to `dev.log` |
| `bun run start` | `python3 main.py`, teeing output to `server.log` |
| `bun run terminal` | `terminal/run.sh` — run the terminal as its own service |
| `bun run test` | `scripts/test.sh` — byte-compile + Python unittest + Node test-runner suites |
| `bun run test:python` | `python3 -m unittest discover -s tests -p 'test_*.py'` |
| `bun run lint` | `python3 -m compileall -q main.py nova routers ui terminal` |
| `bun run db:push` | Run `nova.bootstrap.run_bootstrap()` once (schema sync + seed) |

---

## 🆕 What's new in this release

| Surface | Where | Notes |
| --- | --- | --- |
| Gateway client-key auth | `nova/auth.py` | Bearer / `x-api-key`; 401 once any enabled `ClientKey` exists or `NOVA_REQUIRE_API_KEY` is set. Error codes: `missing_api_key`, `invalid_api_key`, `model_not_allowed`, `rate_limit_exceeded`, `token_budget_exceeded`. |
| Per-(provider, model, key) usage windows | `nova/ratelimit.py`, `UsageWindow` | Minute/day request + token counters gate the candidate pool. Unlimited unless you configure limits. |
| Upstream key cooldown/rotation | `nova/ratelimit.py` | 429/401/5xx cool a key down, honouring `Retry-After`; the highest-weight warm key wins. |
| Named routing profiles | `nova/profiles.py` | `auto` and `auto:<profile>` virtual model ids, resolved before routing. |
| Opt-in response cache | `nova/cache.py` | `NOVA_RESPONSE_CACHE`; TTL + LRU over non-streaming, tool-call-free completions. |
| Sticky sessions | `nova/session.py` | `x-nova-session` (or `metadata.session_id`) pins a conversation to its serving model. |
| OpenAI Responses API | `routers/openai_responses.py` | `POST /v1/responses`, with the streaming events `response.created`, `response.output_text.delta`, `response.completed` and `response.failed`. |
| TTFT + p95 analytics | `GET /api/admin/analytics` | `RequestLog.ttftMs` feeds `ttft_p50_ms`, `ttft_p95_ms` and `p95_latency_ms`. |
| Limits, cache and session admin APIs | `routers/admin_limits.py` | `GET /api/admin/usage`, `GET`/`POST /api/admin/rate-limits`, `GET /api/admin/cache/stats`, `POST /api/admin/cache/clear`, `GET /api/admin/sessions`. |
| Routing profiles admin API | `routers/admin_routing.py` | `GET`/`POST /api/admin/routing/profiles`, `DELETE /api/admin/routing/profiles/{name}`. |
| Curated registry + validator | `nova/registry.py` | `validate()` / `summary()` / `is_keyless()` / `registry_models_for_engine()`; `base_url` is always an API root. |
| Playground tab | `ui/tabs/playground.py` | Sends a chat completion through this gateway and shows the honest `_nova` metadata it comes back with. |
| Dark AI World UI layer | `static/nova-world.css` | Animated aurora background, reveal-on-scroll, hover glow; all motion off under `prefers-reduced-motion`. |
| Exchange provider kind + trading schema | `nova/registry.py`, `nova/models.py` | `exchange` kind; additive `TradeOrder`, `TradeFill`, `RiskPolicy`, `TradeAuditLog` (bootstrap `create_all`, no destructive migration); audit is append-only. Providers of this kind are excluded from every chat/model path and Test is configuration-only. |
| Read-only trading API | `routers/trading.py`, `nova/trading.py` | `GET /v1/trading/quote`, `/positions`, `/balances` (also `/api/v1/*`); Binance + Coinbase Exchange adapters; typed `trading_error`s; no fallback across exchanges. |
| Trading scopes on client keys | `nova/auth.py`, Keys UI | `tradingScopes` (wire `trading_scopes`); empty = no trading access; `trading:read` umbrella plus `trading:quote` / `:positions` / `:balances`. |
| Agent trading read tools | `nova/agent.py`, `routers/agent_api.py` | `trading_quote`, `trading_positions`, `trading_balances` call `nova.trading.read_*`; no order tool exists. |
| Trading kill-switch + audit | Providers tab, `routers/admin_trading.py` | Live armed/halted pill and engage/release button; `GET`/`POST /api/admin/trading`, read-only `GET /api/admin/trading/audit`. |
| Exchange read timeout | `nova/config.py` | `NOVA_TRADING_TIMEOUT` (default `10` seconds) bounds trading reads. |

---

## 🤖 Connect Claude Code (and Claude Desktop)

RouterX speaks the Anthropic Messages API natively, so Claude Code can use the whole provider pool with fallback. Point it at your RouterX base URL:

```bash
export ANTHROPIC_BASE_URL="https://your-routerx-host/v1"     # local: http://localhost:3000/v1
export ANTHROPIC_AUTH_TOKEN="any-value"                       # RouterX auth (optional client key)
export ANTHROPIC_MODEL="anthropic/claude-3-5-sonnet"          # a model id from GET /v1/models
# Claude Code also honours ANTHROPIC_SMALL_FAST_MODEL for lightweight turns.
claude
```

What RouterX implements for the agent loop:

- `POST /v1/messages` — streaming **and** non-streaming, with the full SSE event order (`message_start` → `content_block_start` → `content_block_delta` → `message_delta` → `message_stop`).
- `POST /v1/messages/count_tokens` — Claude Code calls this before every turn; without it the session 404s and dies.
- `tool_use` / `tool_result` blocks are preserved end-to-end (the loop dies on the second turn without them); `thinking`/`redacted_thinking` history is dropped safely.
- If the requested model maps to an **Anthropic-kind provider**, RouterX passes straight through; otherwise it translates to the OpenAI pipeline and back, and the fallback chain still applies.
- Long thinking runs are covered by stream heartbeats, so Claude Code doesn't drop the request mid-answer.

The same endpoints power any Anthropic SDK:

```bash
curl $BASE/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"anthropic/claude-3-5-sonnet","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

#### “Credentials rejected … Status: 403” mid-conversation

This banner is Claude’s rendering of **any** 401/403, including a 403 that was
never produced by RouterX. Check the raw response first:

- RouterX responses are **JSON** and always carry `x-request-id`. A bad or
  disabled client key is a JSON `401 invalid_api_key`; a model outside the
  key’s `allowedModels` is a JSON `403 model_not_allowed`.
- Key cooldowns and lockouts cannot produce this page: they only re-order or
  skip upstreams inside RouterX. If every stage fails, RouterX answers JSON
  `502` (or an SSE `error`), which Claude reports as a different error.
- A response that is **HTML** with a `<title>Blocked</title>` page, a
  `Request ID:` and “Powered by Render” came from **Render’s Cloudflare WAF**,
  not from RouterX. It never reaches the app (no `x-request-id`, no
  `RequestLog` row).

Why it appears mid-conversation and survives Retry/Continue: Claude Code sends
the whole message history every turn. Render’s WAF flags “pipe into an
interpreter” command strings as command injection, so one tool call such as
`baw wallet address --json | python -c "..."` (or any `... | python -c`,
`... ; python -c`, `... | perl -e`) stays in the history and makes **every
later request** 403. A fresh conversation works until the same pattern appears
again; Retry/Continue re-sends the offending history and fails again.

Workarounds, in order of reliability:

1. Point Claude Desktop/Code at a RouterX instance whose edge you control —
   `http://localhost:3000/v1`, a VPS, or a container host without a
   body-inspecting WAF. Render’s WAF is not user-configurable (Render feature
   request: “Let us disable the Cloudflare WAF”).
2. If you must stay on Render, avoid piping into interpreters in tool
   commands: use `jq`, `head`, or write a temporary script and run
   `python script.py` (a bare `python -c` is fine; `| python -c` is not), then
   start a new conversation — an already-poisoned history cannot be retried
   through the WAF.
3. Contact Render support with the WAF page’s `Request ID` and ask them to tune
   the managed rule for your service.

These are deployment/edge fixes: no RouterX code path can emit that HTML page,
because the request is rejected before it reaches the app.

---

## 🔌 Configure providers &amp; the fallback pipeline

In **Providers → Add Provider** choose **Anthropic (native)**, **OpenAI-compatible**, **Cloudflare Worker**, **Custom gateway API**, or **xAI Grok**. A provider's **base URL points upstream** (not RouterX's own `/v1`). Pick **Anthropic (native)** for `https://api.anthropic.com` with an Anthropic key; enter the API **root**, not a full `/chat/completions` path.

Then, in **Routes**, create a chain. The route builder has a **model search**: type to filter the catalogue and click models in order. `auto` (default on) appends **every other healthy, enabled provider** as stand-ins — so a failure rolls onto your pool, not straight onto the free engine.

```bash
# what the router is doing right now, and what it is skipping (and why)
curl -s localhost:3000/api/admin/routing

# create a fallback chain: public id -> ordered upstream models
curl -s -X POST localhost:3000/api/admin/routes -H 'content-type: application/json' \
  -d '{"public_id":"my-model","fallbacks":["anthropic/claude-3-5-sonnet","groq/llama-3.3-70b"],"auto":true}'

# resolve exactly what a request would do (direct / chain / auto stand-ins)
curl -s "localhost:3000/api/admin/routes/preview?model=my-model"
```

Every chat response explains itself: `_nova.routing` carries the strategy, the ordered candidates, what was excluded and why; `_nova.fallback` is `true` when anything other than the direct model served the request.

### Gateway client keys

Client keys (`nova-sk-…`) become real enforcement as soon as one is enabled. On a fresh install with no keys, anonymous requests keep working (fail-open); the moment an enabled key exists — or you set `NOVA_REQUIRE_API_KEY=1` — a request with no credential is a `401 missing_api_key` (fail-closed). An unknown or disabled token is always `401 invalid_api_key`.

```bash
# once a client key exists, every gateway call needs a credential
curl localhost:3000/v1/models -H "Authorization: Bearer nova-sk-..."
curl localhost:3000/v1/models -H "x-api-key: nova-sk-..."
```

Each key can carry an `allowedModels` glob list (403 `model_not_allowed`), an `rpmLimit` (429 `rate_limit_exceeded` + `Retry-After`) and a `tpdLimit` daily token budget (429 `token_budget_exceeded`).

---

## 🧭 Routing

Routing lives in `nova/routing.py` as pure functions over plain rows: policy engine, exposure lists, tag routing, lockouts and the selection strategy are folded into one dispatch plan with an audit trail. Decisions never live in a router; the gateway only supplies rows and records outcomes. Everything is tunable at runtime:

```bash
curl -s -X POST localhost:3000/api/admin/routing -H 'content-type: application/json' -d '{
  "strategy": "weighted",
  "policies": [
    {"id":"no-preview","name":"no preview models","type":"access","priority":1,
     "conditions":{"model_pattern":"gpt-*"},"actions":{"block_model":["gpt-4o"]}},
    {"id":"cap","name":"cap output","type":"budget","priority":9,"actions":{"max_tokens":4096}}
  ],
  "chains": {"my/model": [{"id":"groq/llama-3.3-70b","priority":0,"enabled":true}]},
  "denylist": ["internal/*"]
}'
```

| Knob | What it does |
| --- | --- |
| **Strategy** | `priority` (default), `weighted`, `round-robin`, `random`, `least-used`, `cost-optimized`, `rules`/`cost`/`latency`/`sla`/`lkgp` |
| **Policies** | `access` blocks a model by glob (refused, never silently substituted), `routing` prefers providers, `budget` caps output tokens |
| **Tag routing** | `metadata: {"tags":["eu"],"tag_match_mode":"any"}` limits a request to tagged providers |
| **Cooldowns / lockouts** | A failing (provider, model) is backed off and skipped until it recovers; a success clears it instantly (`/api/admin/routing/availability`) |
| **Exposure lists** | Deny/allow globs hide a model from `/v1/models` **and** every candidate pool — it can never sneak back in via a fallback chain |
| **Rate limits** | The `rate_limits` SystemConfig knob or the `NOVA_DEFAULT_*` env vars: per-provider `rpm`/`tpm`/`rpd`/`tpd`, `0` = unlimited (`/api/admin/rate-limits`) |
| **Profiles** | `auto:<name>` = a stored fallback list plus an optional strategy (`/api/admin/routing/profiles`) |
| **MCP scopes** | `nova/mcp_tools.py` carries the scope table; a credentialed tool is held to the scopes it declares |

### Named routing profiles

`auto` is whatever your default chain and strategy produce. A profile names a concrete alternative for one request — no new model id per upstream:

```bash
curl -s -X POST localhost:3000/api/admin/routing/profiles -H 'content-type: application/json' \
  -d '{"name":"fast","fallbacks":["groq/llama-3.3-70b-versatile"],"strategy":"latency"}'

curl localhost:3000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"auto:fast","messages":[{"role":"user","content":"hi"}]}'
```

The response `model` stays `auto:fast`; the profile only changes *how* the request is routed. An unknown profile is a 404 — never a silent fall back to `auto`.

### Sticky sessions

Send `x-nova-session: <id>` (or `metadata.session_id`) and the first model that answers pins the conversation for `NOVA_SESSION_TTL` seconds. The pin is a preference: it moves the pinned model to the front of the plan and hands off cleanly (with a `_nova.handoff` note) when it fails, so the fallback chain is unchanged.

### Usage windows and rate limits

`nova/ratelimit.py` counts requests and tokens per (provider, model, upstream key) in open minute and day windows (`UsageWindow`), and `route_request` drops any pair whose window is already exhausted before it orders the pool — so a request that would be refused for quota is served by the next candidate instead. Limits are **unlimited by default**: nothing is throttled until you configure it.

```bash
# per-provider overrides; "*" is a default for every provider
curl -s -X POST localhost:3000/api/admin/rate-limits -H 'content-type: application/json' \
  -d '{"rate_limits":{"groq":{"rpm":30,"tpm":6000},"*":{"rpm":120}}}'
```

Upstream keys get their own cooldowns: a 429/401/5xx puts that key to sleep (`NOVA_KEY_COOLDOWN_*`, honouring the provider's `Retry-After`), and the highest-weight key that is not cooling is used instead.

---

## 🔌 Gateway API

```bash
BASE=https://your-routerx-host/v1

curl $BASE/models                             # DB catalogue
curl "$BASE/models?discover=1"                # live discovery from every provider
curl "$BASE/models?discover=1&free=1"         # free-tier only

curl $BASE/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"routerx/air","messages":[{"role":"user","content":"hi"}]}'

curl -N $BASE/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"routerx/air","stream":true,"messages":[{"role":"user","content":"Count to 5"}]}'

curl $BASE/responses -H 'Content-Type: application/json' \
  -d '{"model":"routerx/air","input":"Summarise the last deploy"}'

curl -N $BASE/responses -H 'Content-Type: application/json' \
  -d '{"model":"routerx/air","input":"Count to 5","stream":true}'

curl $BASE/messages -H 'Content-Type: application/json' \
  -d '{"model":"anthropic/claude-3-5-sonnet","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'

curl $BASE/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"text-embedding-3-small","input":"hello world"}'
```

All endpoints exist under both `/v1/*` and `/api/v1/*`. Chat-style responses carry `_nova` metadata (upstream provider, stage, fallback state, routing decision, and TTFT where known). `POST /v1/responses` answers in the OpenAI Responses shape (`object: "response"`, `output[].content[].text`) and streams `response.created`, `response.output_text.delta`, `response.completed`, or `response.failed`. CORS is enabled by default. Admin APIs live under `/api/admin/*`; agent APIs under `/api/agent/*`.

### Read-only trading

Trading is a separate, terminal namespace: **one configured `exchange` provider per read, with no retry and no fallback across exchanges.** Reads are served under both `/v1/trading/*` and `/api/v1/trading/*`.

Every trading gateway read requires a `ClientKey` -- anonymous access is refused even on a fail-open text install (`401 missing_api_key`). The key must carry the endpoint scope (`trading:quote`, `trading:positions`, `trading:balances`, or the `trading:read` umbrella); exact names, `*`, `trading:*` and globs match. The default `tradingScopes` is empty, so existing keys gain no trading access.

```bash
KEY=nova-sk-...
curl -s "$BASE/trading/quote?symbol=BTC/USDT" -H "Authorization: Bearer $KEY"
curl -s "$BASE/trading/quote?symbol=BTC/USDT&exchange=binance" -H "Authorization: Bearer $KEY"
curl -s "$BASE/trading/positions?exchange=binance" -H "Authorization: Bearer $KEY"
curl -s "$BASE/trading/balances?exchange=coinbase" -H "Authorization: Bearer $KEY"
```

Success uses the gateway envelope, with `stage: 1` and `fallback: false`:

```json
{"object":"trading.quote","data":{"exchange":"binance","symbol":"BTC/USDT","base_asset":"BTC","quote_asset":"USDT","bid":123.45,"ask":123.46,"last":123.45,"ts":"2026-10-29T12:00:00Z","source":"..."},"_nova":{"exchange":"binance","provider":"...","fallback":false,"cached":false,"stage":1}}
```

Normalized shapes: quote `{exchange,symbol,base_asset,quote_asset,bid,ask,last,ts,source}`; positions `{exchange,as_of,positions:[{symbol,side,quantity,entry_price,mark_price,unrealized_pnl,leverage}]}`; balances `{exchange,as_of,balances:[{asset,free,locked,total}]}` (zero totals are filtered). Errors are `{"error":{"message":"...","type":"trading_error","code":"..."}}` with `retry_after` and other extras at the top level; codes include `trading_disabled` (503), `exchange_not_configured` (503), `exchange_not_found` (404), `exchange_disabled` (503), `exchange_required` (400), `unsupported_exchange` (501), `positions_unsupported` (501), `exchange_credentials_missing` (503), `invalid_symbol` (400), `exchange_timeout` (504), `exchange_auth_failed` / `exchange_unreachable` / `exchange_error` / `exchange_bad_response` (502), `exchange_rate_limited` (503), `exchange_rejected` (400) and `trading_internal_error` (502).

Adapters: **Binance** uses `/api/v3/ticker/24hr` for spot quotes (or `/fapi/v1/ticker/24hr` on a futures root), signed spot balances and signed futures positions via `/fapi/v2/positionRisk`; spot positions return `501 positions_unsupported`. **Coinbase Exchange** uses `/products/{BASE}-{QUOTE}/ticker` for quotes, signed `/accounts` for balances and returns `501 positions_unsupported`. Any other exchange is `501 unsupported_exchange`. Exactly one provider serves a read; a failure is a typed decision, never a retry on another exchange.

Exchange credentials live on the provider's `apiKey` as JSON `{"api_key":"...","api_secret":"...","passphrase":"..."}` (aliases accepted) or compact `key:secret`; Coinbase requires `passphrase`. Secrets are **plaintext** until Phase 5 encryption lands. `exchange` providers are excluded from chat, discovery, sync, `/v1/models` and admin model paths, and Provider **Test** only checks configuration rather than probing the exchange.

Admin kill-switch and audit (the Providers tab shows a live **Trading armed/halted** pill with engage/release):

```bash
curl -s localhost:3000/api/admin/trading
curl -s -X POST localhost:3000/api/admin/trading -H 'content-type: application/json' -d '{"kill_switch":true}'
curl -s "localhost:3000/api/admin/trading/audit?limit=50"
```

The audit endpoint is read-only: there is no update or delete route.

> **No order execution yet.** These endpoints are read-only. Phase 2 (paper/sandbox execution) and live orders are not built; nothing creates, signs, submits or relays an order.

### Observability and admin surfaces

```bash
curl -s localhost:3000/api/admin/usage            # intake today, open windows, key cooldowns
curl -s localhost:3000/api/admin/rate-limits      # current knob + env defaults
curl -s localhost:3000/api/admin/cache/stats      # cache counters + configuration
curl -s -X POST localhost:3000/api/admin/cache/clear
curl -s localhost:3000/api/admin/sessions         # live sticky-session pins
curl -s localhost:3000/api/admin/analytics        # latency p50/p95 + TTFT p50/p95
```

The response cache is **off** unless `NOVA_RESPONSE_CACHE=on` (or the `response_cache` SystemConfig knob). It only stores non-streaming 200 completions with no tool calls, and `x-nova-no-cache: 1` bypasses both read and write.

---

## 🖥️ Using the RouterX Console

Everything lives in one chatbox — input is routed automatically:

| You type | What happens |
| --- | --- |
| `Why is my fallback chain failing?` | Gateway AI answers in chat, with live provider/model telemetry chips |
| `$ free -m` | Runs a real command in the sandbox executor, shows output + exit code |
| `$ nova gpu kaggle on` | Attaches a GPU from the Kaggle pool to your compute config |
| `/boost 256`, `/models`, `/gpu`, `/storage`, `/help` | Slash shortcuts for terminal-side config |
| `! Audit the storage providers and report failures` | Delegates to the autonomous agent — steps stream live until it completes |
| `/plugins` | Opens the MCP server drawer — add, test, disable/remove, and run a tool by hand |

The **Playground** tab sends a completion through this same gateway and shows the raw `_nova` metadata beside the answer, so you can see which provider answered and whether it was a fallback.

---

## 🔌 MCP plugins

The **Plugins** button (or `/plugins`) registers [Model Context Protocol](https://modelcontextprotocol.io) servers. Everything you add is additive — rows are never seeded and never wiped by a deploy.

| Transport | Example | How it runs |
| --- | --- | --- |
| `http` | `https://mcp.example.com/mcp` | Streamable HTTP JSON-RPC, plain-JSON or SSE-framed replies |
| `stdio` | `npx -y @modelcontextprotocol/server-filesystem /app/build` | A local plugin process (argv-split, never a shell), one cached child per server |

Tools join the permanent toolbox, so an agent task can call them the same way it calls the terminal. Headers are stored as credentials, shown masked, and a masked re-save keeps the value.

```bash
curl -X POST localhost:3000/api/admin/mcp/servers/test -H 'Content-Type: application/json' -d '{"id":"deepwiki"}'
curl localhost:3000/api/admin/mcp/tools
```

---

## ⬛ Terminal — the `~ Root@Build` cloud workspace

The sidebar's **RouterX Console** entry opens the merged cloud workspace at **`/terminal`** in a new tab: the chat panel on the left (chat, `$ commands`, `! agent tasks`, `/ shortcuts`) and the cloud terminal on the right.

- **Permanent cloud shell** — every session is a real `bash` on a pseudo-terminal with root access on Debian, branded `~ Root@Build:<cwd>#`.
- **Session tabs** — up to 8 live shells; switch, rename (double-click) or close them.
- **Live state** — OSC 7 working-directory reporting and full ANSI colour via xterm.js.
- **Cloud build config** — add/edit storage targets (R2, GCS, S3, B2, Azure, custom S3) from the UI; secrets stored server-side and always shown masked.

```bash
curl -s localhost:3000/api/admin/terminal/pty/sessions
curl -sN "localhost:3000/api/admin/terminal/pty/stream?session=<id>"
curl -s localhost:3000/api/build/info
```

### Hosting the terminal separately

The whole terminal is one directory, `terminal/`, that imports nothing from `nova/` and needs no database — so hosting it on its own is a deployment decision, not a code change. With `NOVA_TERMINAL_URL` unset it runs in-process (the default).

```bash
docker build -t routerx-terminal ./terminal
docker run -p 3100:3100 -e NOVA_TERMINAL_TOKEN=<shared-secret> routerx-terminal

# then point the gateway at it
NOVA_TERMINAL_URL=http://terminal-host:3100 NOVA_TERMINAL_TOKEN=<shared-secret> bun run dev
```

| Variable | Where | What it does |
| --- | --- | --- |
| `NOVA_TERMINAL_URL` | gateway | Host the terminal separately. Unset = in-process |
| `NOVA_TERMINAL_TOKEN` | **both** | Shared secret for `X-Nova-Terminal-Token`. Gates real root shells — always set it before exposing the terminal beyond loopback |
| `NOVA_TERMINAL_PORT` | terminal | The terminal's own port (default `3100`) |
| `NOVA_BUILD_ROOT` | terminal | Where shells start (default `/app/build`) |
| `NOVA_AGENTBOX_BASE_URL` / `_API_KEY` / `_MODEL` | terminal | Point the standalone Agentbox agent at any OpenAI-compatible endpoint |

`terminal/` ships its own console and AI agent (**Agentbox**), so a terminal deployed alone still gives you shells *and* an assistant.

---

## 🔒 Configuration &amp; notes

Key environment variables (see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `3000` | Bind port (`0.0.0.0:$PORT`) |
| `DATABASE_URL` | SQLite `db/custom.db` | SQLite or Postgres (`postgres://…` accepted) |
| `PUBLIC_BASE_URL` | request origin | Advertised base URL in `/api/admin/meta` and client snippets |
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated origin allow-list |
| `NOVA_ENGINE_PORT` | `3099` | RouterX Free engine sidecar port (never the app port) |
| `NOVA_UPSTREAM_READ_TIMEOUT` | `600` | Max silence between upstream bytes (long thinking runs) |
| `NOVA_SSE_HEARTBEAT` | `10` | Seconds between keep-alive comments while an upstream is silent |
| `NOVA_AUTO_STANDINS` | `6` | Max auto stand-in providers appended to a chain |
| `NOVA_CONNECT_TIMEOUT` | `5` | Upstream connect timeout (seconds) |
| `NOVA_NONSTREAM_READ_TIMEOUT` | `300` | Read timeout for non-streaming upstream calls |
| `NOVA_TRADING_TIMEOUT` | `10` | Exchange read timeout for trading quotes, positions and balances (seconds) |
| `NOVA_SEED_REGISTRY` | `on` | Seed the curated catalogue on boot (`off` skips it) |
| `NOVA_REQUIRE_API_KEY` | off | Refuse anonymous gateway requests even when no client keys exist |
| `NOVA_DEFAULT_RPM` / `_TPM` / `_RPD` / `_TPD` | `0` (unlimited) | Per-(provider, model) request / token caps |
| `NOVA_KEY_COOLDOWN_429` | `60` | Cooldown seconds for an upstream 429 with no `Retry-After` |
| `NOVA_KEY_COOLDOWN_5XX` | `30` | Cooldown seconds for an upstream 5xx |
| `NOVA_KEY_COOLDOWN_401` | `300` | Cooldown seconds for an upstream 401/403 |
| `NOVA_KEY_COOLDOWN_MAX` | `900` | Ceiling for any key cooldown |
| `NOVA_RESPONSE_CACHE` | off | Enable the non-streaming response cache |
| `NOVA_RESPONSE_CACHE_TTL` | `300` | Cache entry lifetime (seconds) |
| `NOVA_RESPONSE_CACHE_MAX` | `256` | Maximum cached entries |
| `NOVA_SESSION_TTL` | `1800` | Sticky-session pin lifetime (seconds) |
| `NOVA_UI_GATEWAY_TOKEN` | unset | Client key the dashboard presents to its own gateway (needed once client keys exist) |

**Data:** no mock/demo data ships with the project. The dashboard starts with the built-in engine and the curated catalogue (providers disabled, no keys), and fills with **real** data as you enable providers, add keys, sync catalogues, and use the gateway. Everything you add survives every restart and every `git push`.

---

---

## 🗺️ Roadmap

**The direction: RouterX's agent will be able to execute trades through a connected trading provider, so an LLM in the dashboard — or an external client through the gateway — can place and manage positions.** Trading is designed onto the seams this codebase already has: gateway endpoints under `/v1` + `/api/v1`, admin routers under `/api/admin`, pure routing decisions in `nova/routing.py`, the agent's `ALLOWED_ACTIONS` registry in `nova/agent.py`, the provider/model catalogue, `ClientKey` scopes, the MCP tool registry (`nova/mcp_registry.py`) and the console.

### ⚠️ Status and risk

**Phase 0 and Phase 1 are implemented; Phase 2 and later are not built.** Trading today is **read-only**: quotes, positions and balances from one configured exchange, with a kill-switch, append-only audit and client-key scopes. **Nothing in this repository executes, signs or relays an order today.** When live execution ships, it carries **real financial risk**: an LLM can be wrong, a market can gap, and an order cannot be un-sent. Every phase below is a precondition for the next, and the kill-switch landed in Phase 0 gates all of it.

### Phase 0 — Foundations: risk schema, audit, kill-switch *(implemented)*

Landed: the `exchange` provider kind; `TradeOrder`, `TradeFill`, `RiskPolicy` and `TradeAuditLog` tables (additive); an append-only audit; and a global kill-switch that refuses every trading entry point with `trading_disabled`.

- An `exchange` provider **kind** alongside `openai` / `anthropic` / `gemini`, registered in `nova/registry.py` and the provider kinds table.
- Additive schema: `TradeOrder`, `TradeFill`, `RiskPolicy` and `TradeAuditLog` rows in `nova/models.py` (bootstrap adds them; no destructive migration).
- An append-only trade audit log: every trading decision and refusal is a row that is never updated or deleted.
- A global kill-switch admin toggle; when engaged, **every** trade tool refuses with a typed error, on every host.

*Why it matters:* you cannot add safety to a live order after the fact. Audit and a kill-switch have to exist before the first order exists.

### Phase 1 — Read-only market and account data *(implemented, read-only)*

Landed: the three GET endpoints under both prefixes, normalized shapes, Binance and Coinbase Exchange adapters, per-client-key trading scopes, and the agent read tools. Coinbase positions and Binance spot positions return `positions_unsupported` (501); an unknown exchange returns `unsupported_exchange` (501).

- Normalised gateway endpoints: `/v1/trading/quote`, `/v1/trading/positions`, `/v1/trading/balances` (also under `/api/v1/*`), one shape across exchanges.
- New agent read tools in `nova/agent.py` (`ALLOWED_ACTIONS` + executors) so the agent can reason over the market before it can act on it.
- Per-`ClientKey` read scopes for the trading namespace.

*Why it matters:* an agent that cannot read a balance or a quote cannot make a defensible decision, and read tools are harmless to expose first.

### Phase 2 — Sandbox execution *(not implemented)*

- A paper-trading adapter that speaks the **exact same wire contract** as live, so the agent loop, the tools and the UI are developed against it unchanged.
- A dry-run flag, mandatory by default; a sandbox order is recorded as a `TradeFill` with no upstream call.
- The console/agent gains an order-preview step backed by the sandbox adapter.

*Why it matters:* the difference between paper and live must be one adapter, not a second code path — that is how a bug hides until it costs money.

### Phase 3 — Live execution with hard guardrails *(not implemented)*

- Per-order max notional, per-day loss cap and a symbol allow-list, all evaluated as pure functions before dispatch.
- A confirmation policy: orders above a threshold require human-in-the-loop approval, surfaced in the console.
- Idempotency keys, so a retried request can never double-fill.
- Typed errors — `insufficient_funds`, `market_closed`, `risk_rejected` — and the key invariant below.

**Invariant: a trade stage is terminal.** The gateway's fallback chain re-sends a request to the next provider on failure, which is correct for text generation and catastrophic for money. A trading request must **never** be re-sent to a second provider: if the first exchange rejects or times out, the result is a decision, not a retry.

*Why it matters:* money is not idempotent by default. A blind retry across providers can place the same order twice.

### Phase 4 — Strategy and signals

- Scheduled strategy runs (the existing task/step loop with a schedule rather than a prompt).
- Backtest hooks that replay the same signal → order pipeline against historical data.
- A signal → order pipeline that goes through the Phase 3 guardrails, never around them.
- Trading tools exposed through the MCP registry, so external agents can call them with the same scopes.

*Why it matters:* automation without a backtest and a guardrail path is just a faster way to lose money.

### Phase 5 — Operations and compliance

- Dashboards for P&L, exposure and rejected orders.
- Exchange keys encrypted at rest under `NOVA_ENCRYPTION_KEY`.
- 2FA required to enable live mode.
- Per-client-key trading **limits** (beyond the read scopes shipped in Phase 1), so a dashboard agent and an external client can hold different limits.

*Why it matters:* once real money and real API keys are on the box, key custody, a second factor and an exposure view stop being nice-to-haves.

## 🗂️ Project structure

```
main.py            FastAPI entrypoint — 0.0.0.0:$PORT, CORS, /health, API routers, UI
terminal/          the ENTIRE terminal feature, one path, hostable on its own:
                   pty (real PTY sessions) · sandbox (allowlist exec) · link
                   (in-process vs separately hosted) · api (the contract, mounted by
                   BOTH hosts) · agentbox (the AI agent) · web (the console page at `/`)
                   · service (the standalone host) · config · run.sh · Dockerfile
nova/              config, SQLAlchemy models (incl. TradeOrder / TradeFill /
                   RiskPolicy / TradeAuditLog), bootstrap, engine sidecar client,
                   live discovery, model sync, agent runtime, storage lib, MCP
                   client + registry; the routing brain (routing / router_settings
                   / profiles); the gateway control modules (auth / ratelimit /
                   cache / session); read-only trading (trading.py / trading_audit.py /
                   trading_settings.py); the catalogue accessor (catalog / registry)
                   and data/*.json
routers/           gateway (/v1 + /api/v1, incl. openai_responses + the
                   anthropic_adapter at /v1/messages + trading.py reads), admin CRUD,
                   admin_limits (usage / rate-limits / cache / sessions), admin_trading.py
                   (kill-switch / audit), terminal glue, compute, storage, agent,
                   MCP registry, build APIs
ui/                the Python frontend module — shell + 11 dashboard tabs, server-
                   rendered fragments consumed by HTMX (BFF over the JSON API);
                   tabs live in ui/tabs/ (incl. playground); /terminal is full-page
templates/         Jinja2 templates (base shell, tab fragments, partials)
static/            nova.css, nova-world.css, app.js, terminal_workspace.js, logo
engine/            RouterX Free engine sidecar (bun/node, zero npm deps) + free-models.json
docs/              architecture.svg · fallback-demo.svg · screenshots/
db/                SQLite database file (created at first boot)
Dockerfile         python:3.12-slim runtime + sidecar — no Node build
docker-compose.yml one-command deploy with persistent volume — gateway + terminal host
```

## 🧪 Tests

```bash
sh scripts/test.sh          # byte-compile → Python suite → Node test-runner suites
python3 -m unittest discover -s tests -p 'test_*.py'
```

Status: green — 450 tests, 418 passed, 0 failures, 0 errors, 32 skipped (the skips are POSIX-only PTY and file-mode tests, which do not run on Windows). Fully offline: provider protocol tests use `httpx.MockTransport`, and the gateway needs no keys. The Node suites (`tests/*.cjs`) cover dashboard navigation (`test_app_navigation.cjs`), the terminal workspace (`test_terminal_workspace.cjs`) and the standalone terminal console (`test_terminal_console.cjs`); `scripts/test.sh` runs them with `node --test` when Node is available.

## 📄 License

MIT — see [LICENSE](LICENSE).
