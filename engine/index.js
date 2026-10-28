/**
 * RouterX Free engine sidecar — the built-in free-model gateway.
 *
 * This is the localhost HTTP bridge between the Python gateway and the bundled
 * free-model catalogue in engine/free-models.json.
 * It needs NO configuration and NO API key: keyless providers (Pollinations,
 * OVHcloud) are used by default, and OpenRouter/ZeroLimitAI are used
 * automatically as soon as their key is present in the environment.
 *
 *   POST /chat     {messages, stream?, model?, tools?, tool_choice?} → OpenAI-shaped JSON or SSE
 *   POST /search   {query}   → {results: [{url, name, snippet, host_name, date}]}
 *   POST /read_url {url}     → {title, text, published_time, url}
 *   GET  /models             → the free model catalogue
 *   GET  /health             → 200 {ok:true} when at least one provider is usable
 *
 * Runs on Bun (preferred) or Node >=18 — both are auto-detected.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HOST = '127.0.0.1';
const PORT = Number(process.env.ENGINE_PORT || 3099);

const HERE = dirname(fileURLToPath(import.meta.url));
const UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36';

/* ------------------------------ catalogue ------------------------------ */

function loadCatalogue() {
  const raw = readFileSync(join(HERE, 'free-models.json'), 'utf8');
  const data = JSON.parse(raw);
  return {
    updatedAt: data.updated_at || null,
    sourceRepo: data.source || 'RouterX free-model catalogue',
    providers: data.providers || {},
    models: Array.isArray(data.models) ? data.models : [],
  };
}

let CATALOGUE = loadCatalogue();

// Re-read on demand (the Python side can refresh the snapshot on disk).
function reloadCatalogue() {
  try {
    CATALOGUE = loadCatalogue();
  } catch {
    /* keep the last good catalogue */
  }
  return CATALOGUE;
}

function chatModels() {
  return CATALOGUE.models.filter((m) => m.kind === 'chat' && m.route);
}

function modelById(id) {
  if (!id) return null;
  const needle = String(id).trim().toLowerCase();
  return (
    CATALOGUE.models.find((m) => String(m.id).toLowerCase() === needle) ||
    CATALOGUE.models.find((m) => String(m.name).toLowerCase() === needle) ||
    null
  );
}

/**
 * Friendly built-in tiers. Each maps to the preferred free models (best first),
 * so `routerx/air` (and the legacy `nova/*` ids) keep working while the actual model
 * that answers comes from the free catalogue.
 */
const ALIASES = {
  'routerx/mini': ['pollinations/openai-fast', 'liquid/lfm-2.5-2.6b:free'],
  'routerx/air': [
    'qwen/qwen3.8-27b:free',
    'google/gemma-4-31b-it:free',
    'pollinations/openai',
  ],
  'routerx/pro': [
    'nvidia/nemotron-3-ultra-550b-a55b:free',
    'thinkingmachines/inkling:free',
    'qwen/qwen3.8-27b:free',
    'pollinations/openai',
  ],
  // Legacy ids kept so existing clients/deployments keep working unchanged.
  'nova/mini': ['pollinations/openai-fast', 'liquid/lfm-2.5-2.6b:free'],
  'nova/air': [
    'qwen/qwen3.8-27b:free',
    'google/gemma-4-31b-it:free',
    'pollinations/openai',
  ],
  'nova/pro': [
    'nvidia/nemotron-3-ultra-550b-a55b:free',
    'thinkingmachines/inkling:free',
    'qwen/qwen3.8-27b:free',
    'pollinations/openai',
  ],
};

function provider(route) {
  return (route && CATALOGUE.providers[route.provider]) || null;
}

function providerKey(route) {
  const p = provider(route);
  if (!p) return null;
  return p.key_env ? process.env[p.key_env] || null : null;
}

function providerReady(route) {
  const p = provider(route);
  if (!p) return false;
  if (p.keyless) return true;
  return Boolean(providerKey(route));
}

function sameRoute(a, b) {
  return a && b && a.provider === b.provider && a.model === b.model;
}

/**
 * Ordered provider chain for a request:
 *   1. the exact model the caller asked for (when available)
 *   2. the requested built-in tier alias, best free model first
 *   3. the whole catalogue in quality order
 *   4. a guaranteed keyless last resort
 */
function candidates(requested) {
  const out = [];
  const push = (route) => {
    if (route && route.provider && route.model && providerReady(route) && !out.some((r) => sameRoute(r, route))) {
      out.push(route);
    }
  };

  const asked = modelById(requested);
  if (asked && asked.route) push(asked.route);

  const alias = ALIASES[String(requested || '').trim().toLowerCase()];
  if (alias) for (const id of alias) push(modelById(id)?.route);

  for (const m of chatModels()) push(m.route);

  push(modelById('pollinations/openai')?.route);
  push(modelById('pollinations/openai-fast')?.route);
  return out;
}

/* ------------------------------ utilities ------------------------------ */

function json(status, body, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...(extraHeaders || {}) },
  });
}

function novaMeta(route) {
  const p = provider(route);
  return {
    upstream_model: route.model,
    provider: (p && p.name) || route.provider,
    provider_key: route.provider,
    engine: 'novafree',
    source: CATALOGUE.sourceRepo,
  };
}

function metaHeaders(meta) {
  return {
    'X-Nova-Model': String(meta.upstream_model || ''),
    'X-Nova-Provider': String(meta.provider || ''),
  };
}

async function fetchWithTimeout(url, init, timeoutMs) {
  const signal = AbortSignal.timeout(timeoutMs);
  return fetch(url, { ...init, signal });
}

function decodeEntities(text) {
  return String(text || '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&#x2F;/g, '/');
}

function stripTags(html) {
  return decodeEntities(
    String(html || '')
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
  )
    .replace(/\s+/g, ' ')
    .trim();
}

/* -------------------------------- /health -------------------------------- */

async function handleHealth() {
  const ready = Object.entries(CATALOGUE.providers)
    .filter(([key, p]) => p.keyless || (p.key_env && process.env[p.key_env]))
    .map(([key]) => key);
  return json(200, {
    ok: ready.length > 0,
    engine: 'novafree',
    source: CATALOGUE.sourceRepo,
    updated_at: CATALOGUE.updatedAt,
    providers: ready,
    models: chatModels().length,
  });
}

/* -------------------------------- /models -------------------------------- */

async function handleModels() {
  const data = chatModels().map((m) => ({
    id: m.id,
    object: 'model',
    owned_by: 'novafree',
    name: m.name,
    provider: m.provider,
    context_window: m.context_window ?? null,
    max_output: m.max_output ?? null,
    modalities: m.modalities || ['text'],
    rate_limit: m.rate_limit || null,
    quality: m.quality ?? m.zo_score ?? null,
    health: m.health ?? null,
    is_free: true,
    route: m.route,
  }));
  return json(200, {
    object: 'list',
    engine: 'novafree',
    source: CATALOGUE.sourceRepo,
    updated_at: CATALOGUE.updatedAt,
    data,
  });
}

/* --------------------------------- /chat --------------------------------- */

function upstreamPayload(body, route, stream) {
  const payload = {
    model: route.model,
    messages: body.messages,
  };
  if (stream) payload.stream = true;
  for (const field of ['temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'stop', 'seed', 'response_format', 'tools', 'tool_choice', 'parallel_tool_calls', 'frequency_penalty', 'presence_penalty']) {
    if (body[field] !== undefined && body[field] !== null) payload[field] = body[field];
  }
  return payload;
}

async function callProvider(route, body, stream) {
  const p = provider(route);
  if (!p || !p.chat_url) throw new Error(`unknown provider for route ${JSON.stringify(route)}`);
  const headers = { 'Content-Type': 'application/json' };
  const key = providerKey(route);
  if (key) headers.Authorization = `Bearer ${key}`;
  return fetchWithTimeout(
    p.chat_url,
    { method: 'POST', headers, body: JSON.stringify(upstreamPayload(body, route, stream)), redirect: 'follow' },
    stream ? 600_000 : 180_000
  );
}

function sseStreamFromJson(data, meta) {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      const send = (obj) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(obj)}\n\n`));
      const choice = (data.choices || [])[0] || {};
      const message = choice.message || {};
      send({
        id: data.id || `chatcmpl-${Date.now().toString(36)}`,
        object: 'chat.completion.chunk',
        created: data.created || Math.floor(Date.now() / 1000),
        model: meta.upstream_model,
        _nova: meta,
        choices: [{ index: 0, delta: { role: 'assistant', content: message.content ?? '' }, finish_reason: null }],
      });
      if (message.tool_calls) {
        send({
          id: data.id,
          object: 'chat.completion.chunk',
          created: data.created,
          model: meta.upstream_model,
          _nova: meta,
          choices: [{ index: 0, delta: { tool_calls: message.tool_calls }, finish_reason: null }],
        });
      }
      send({
        id: data.id,
        object: 'chat.completion.chunk',
        created: data.created,
        model: meta.upstream_model,
        _nova: meta,
        choices: [{ index: 0, delta: {}, finish_reason: choice.finish_reason || (message.tool_calls ? 'tool_calls' : 'stop') }],
      });
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return new Response(body, {
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      ...metaHeaders(meta),
    },
  });
}

function ssePassthrough(upstream, meta) {
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const body = new ReadableStream({
    async start(controller) {
      const send = (obj) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(obj)}\n\n`));
      let buf = '';
      try {
        const reader = upstream.getReader();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf('\n')) !== -1) {
            const line = buf.slice(0, idx).replace(/\r$/, '').trim();
            buf = buf.slice(idx + 1);
            if (!line.startsWith('data:')) continue; // ignore comments / event: lines
            const payload = line.slice(5).trim();
            if (!payload || payload === '[DONE]') continue; // we append our own DONE
            let obj;
            try {
              obj = JSON.parse(payload);
            } catch {
              continue;
            }
            if (obj && typeof obj === 'object') {
              obj._nova = meta;
              send(obj);
            }
          }
        }
      } catch (err) {
        send({ error: String((err && err.message) || err) });
      } finally {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      }
    },
  });
  return new Response(body, {
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      ...metaHeaders(meta),
    },
  });
}

async function handleChat(req) {
  let body;
  try {
    body = await req.json();
  } catch {
    return json(400, { error: 'invalid JSON body' });
  }
  if (!Array.isArray(body.messages) || body.messages.length === 0) {
    return json(400, { error: "missing 'messages'" });
  }

  const stream = body.stream === true;
  const requested = typeof body.model === 'string' ? body.model.trim() : '';
  const chain = candidates(requested);
  if (chain.length === 0) {
    return json(502, { error: 'no free model provider is available' });
  }

  const attempts = [];
  for (const route of chain) {
    const meta = novaMeta(route);
    let res;
    try {
      res = await callProvider(route, body, stream);
    } catch (err) {
      attempts.push(`${route.provider}:${route.model} → ${(err && err.message) || err}`);
      continue;
    }

    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      attempts.push(`${route.provider}:${route.model} → HTTP ${res.status} ${detail.slice(0, 120)}`);
      continue;
    }

    if (!stream) {
      let data;
      try {
        data = await res.json();
      } catch {
        attempts.push(`${route.provider}:${route.model} → unparseable response`);
        continue;
      }
      if (!data || !Array.isArray(data.choices) || data.choices.length === 0) {
        attempts.push(`${route.provider}:${route.model} → empty completion`);
        continue;
      }
      data._nova = meta;
      return json(200, data, metaHeaders(meta));
    }

    const ctype = res.headers.get('content-type') || '';
    if (ctype.includes('text/event-stream') && res.body) {
      return ssePassthrough(res.body, meta);
    }
    // Upstream ignored stream:true — degrade to a single-chunk SSE response.
    let data;
    try {
      data = await res.json();
    } catch {
      attempts.push(`${route.provider}:${route.model} → unparseable stream`);
      continue;
    }
    if (!data || !Array.isArray(data.choices) || data.choices.length === 0) {
      attempts.push(`${route.provider}:${route.model} → empty completion`);
      continue;
    }
    return sseStreamFromJson(data, meta);
  }

  return json(502, {
    error: `all free model providers failed: ${attempts.slice(-4).join(' | ') || 'unknown error'}`,
    tried: attempts.length,
  });
}

/* -------------------------------- /search -------------------------------- */

function ddgHref(href) {
  const raw = decodeEntities(href || '');
  const match = /[?&]uddg=([^&]+)/.exec(raw);
  if (match) {
    try {
      return decodeURIComponent(match[1]);
    } catch {
      /* fall through */
    }
  }
  if (raw.startsWith('//')) return `https:${raw}`;
  return raw;
}

function parseDuckDuckGo(html) {
  const titles = [];
  const reLink = /<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = reLink.exec(html)) !== null) {
    const url = ddgHref(m[1]);
    const name = stripTags(m[2]);
    if (url && name) titles.push({ url, name });
  }

  const snippets = [];
  const reSnip = /class="[^"]*result__snippet[^"]*"[^>]*>([\s\S]*?)<\/a>/gi;
  while ((m = reSnip.exec(html)) !== null) snippets.push(stripTags(m[1]));

  return titles.slice(0, 10).map((t, i) => {
    let host = '';
    try {
      host = new URL(t.url).hostname.replace(/^www\./, '');
    } catch {
      host = '';
    }
    return {
      name: t.name,
      url: t.url,
      snippet: (snippets[i] || '').slice(0, 240),
      host_name: host,
      date: null,
    };
  });
}

async function webSearch(query) {
  const out = [];
  try {
    const res = await fetchWithTimeout(
      'https://html.duckduckgo.com/html/',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'User-Agent': UA,
          Accept: 'text/html',
        },
        body: new URLSearchParams({ q: query, kl: 'wt-wt' }).toString(),
      },
      15_000
    );
    if (res.ok) out.push(...parseDuckDuckGo(await res.text()));
  } catch {
    /* fall through to the next source */
  }

  if (out.length === 0) {
    try {
      const url =
        'https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&origin=*&srlimit=6&srsearch=' +
        encodeURIComponent(query);
      const res = await fetchWithTimeout(url, { headers: { 'User-Agent': UA, Accept: 'application/json' } }, 12_000);
      if (res.ok) {
        const data = await res.json();
        for (const r of (data && data.query && data.query.search) || []) {
          const title = String(r.title || '');
          out.push({
            name: title,
            url: `https://en.wikipedia.org/wiki/${encodeURIComponent(title.replace(/ /g, '_'))}`,
            snippet: stripTags(r.snippet || '').slice(0, 240),
            host_name: 'en.wikipedia.org',
            date: null,
          });
        }
      }
    } catch {
      /* no network at all — return the honest fallback below */
    }
  }

  if (out.length === 0) {
    out.push({
      name: `Search the web for “${query}”`,
      url: `https://duckduckgo.com/?q=${encodeURIComponent(query)}`,
      snippet: 'Live search was unavailable from this host — open the link to search manually.',
      host_name: 'duckduckgo.com',
      date: null,
    });
  }
  return out.slice(0, 8);
}

async function handleSearch(req) {
  let body;
  try {
    body = await req.json();
  } catch {
    return json(400, { error: 'invalid JSON body' });
  }
  const query = typeof body.query === 'string' ? body.query.trim() : '';
  if (!query) return json(400, { error: "missing 'query'" });
  try {
    return json(200, { results: await webSearch(query) });
  } catch (err) {
    return json(502, { error: String((err && err.message) || err) });
  }
}

/* ------------------------------- /read_url ------------------------------- */

async function handleReadUrl(req) {
  let body;
  try {
    body = await req.json();
  } catch {
    return json(400, { error: 'invalid JSON body' });
  }
  const url = typeof body.url === 'string' ? body.url.trim() : '';
  if (!/^https?:\/\//i.test(url)) {
    return json(400, { error: "missing or invalid 'url'" });
  }
  try {
    const res = await fetchWithTimeout(
      url,
      { headers: { 'User-Agent': UA, Accept: 'text/html,application/xhtml+xml,text/plain' }, redirect: 'follow' },
      25_000
    );
    if (!res.ok) return json(502, { error: `HTTP ${res.status}` });
    const html = await res.text();
    const titleMatch = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
    return json(200, {
      title: decodeEntities(titleMatch ? titleMatch[1] : url).trim(),
      text: stripTags(html).slice(0, 12_000),
      published_time: null,
      url: res.url || url,
    });
  } catch (err) {
    return json(502, { error: String((err && err.message) || err) });
  }
}

/* --------------------------------- server --------------------------------- */

async function route(req) {
  const url = new URL(req.url);
  try {
    if (req.method === 'GET' && url.pathname === '/health') return await handleHealth();
    if (req.method === 'GET' && url.pathname === '/models') {
      reloadCatalogue();
      return await handleModels();
    }
    if (req.method === 'POST' && url.pathname === '/chat') return await handleChat(req);
    if (req.method === 'POST' && url.pathname === '/search') return await handleSearch(req);
    if (req.method === 'POST' && url.pathname === '/read_url') return await handleReadUrl(req);
    return json(404, { error: 'not found' });
  } catch (err) {
    return json(500, { error: String((err && err.message) || err) });
  }
}

if (typeof Bun !== 'undefined') {
  Bun.serve({ port: PORT, hostname: HOST, fetch: route });
  console.log(`[engine] novafree sidecar (RouterX Free) listening on http://${HOST}:${PORT}`);
} else {
  const { createServer } = await import('node:http');
  const server = createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const hasBody = req.method !== 'GET' && req.method !== 'HEAD' && chunks.length > 0;
      const request = new Request(`http://${HOST}:${PORT}${req.url}`, {
        method: req.method,
        headers: req.headers,
        body: hasBody ? Buffer.concat(chunks) : undefined,
      });
      const response = await route(request);
      const headers = {};
      response.headers.forEach((value, key) => {
        headers[key] = value;
      });
      res.writeHead(response.status, headers);
      if (response.body) {
        for await (const chunk of response.body) res.write(chunk);
      }
      res.end();
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: String((err && err.message) || err) }));
    }
  });
  server.listen(PORT, HOST, () => {
    console.log(`[engine] novafree sidecar (RouterX Free) listening on http://${HOST}:${PORT}`);
  });
}
