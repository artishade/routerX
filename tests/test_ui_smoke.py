"""UI smoke tests — every dashboard fragment renders, empty or with data.

The dashboard is a BFF: a fragment endpoint asks the app's own JSON API
(`ui/api_client`). Here that client becomes a tolerant in-process double — the
returns the tab modules rely on when a fresh install (or an error state) has no
rows — so the fragments render with an EMPTY database and never touch the
network or the developer's data.

Coverage:
  * GET /partials/tab/<key> for every registered tab           -> 200, non-empty, no `{{`
  * the same tabs again with one provider, model, key, route and log row
  * the sub-fragments and BFF actions that build their own payloads
  * POST /ui/playground/send with a mocked gateway (chips + escaping)
  * the shell, every template (parse + include resolution), duplicate ids,
    icon-only buttons without an accessible name
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import unittest
from pathlib import Path
from typing import Any

from ui import api_client
from ui.render import env
from ui.shell import shell_router
from ui.tabs import ui_router

# Tab modules do `from ui.api_client import api`, so each one holds its own
# reference; patch every holder, not just the module attribute.
_TAB_MODULES = (
    "ui.tabs.analytics", "ui.tabs.console", "ui.tabs.keys", "ui.tabs.logs",
    "ui.tabs.models", "ui.tabs.overview", "ui.tabs.playground",
    "ui.tabs.providers", "ui.tabs.routes", "ui.tabs.storage", "ui.tabs.terminal",
)

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

TAB_KEYS = (
    "overview", "playground", "console", "providers", "models", "routes",
    "keys", "storage", "analytics", "logs", "terminal",
)

STATS = {
    "total_requests": 4, "total_tokens": 120, "total_tokens_in": 70,
    "total_tokens_out": 50, "active_keys": 1, "active_models": 3,
    "dead_models": 0, "healthy_models": 1, "providers_count": 2,
    "connected_providers": 1, "cache_hit_rate": 0, "avg_latency_ms": 12.5,
    "error_rate": 0.0, "uptime_s": 60,
    "memory": {"heap_used_mb": 1, "heap_total_mb": 2, "rss_mb": 3, "pct": 50},
    "v8": {"heap_mb": 2048, "swap_mb": 2048},
}

META = {
    "version": "2.0.0", "base_url": "http://127.0.0.1:3000/v1",
    "storage": "hybrid (local + cloud)",
    "fallback": {"auto": True, "spoof_model": True, "max": 3},
    "cooldowns": {"429": 60, "402": 300, "5xx": 30},
    "scheduler": {"check_interval": 3600}, "hedging": {"delay": 2},
    "cache": {"ttl": 600, "max_entries": 1000},
    "limits": {"file_max_mb": 25, "batch_max_items": 50000},
    "presets": [{"key": "groq", "name": "Groq", "kind": "openai"}],
    "kinds": ["openai", "anthropic", "gemini", "builtin"],
    "statuses": ["healthy", "cooling", "dead", "unknown"],
    "profiles": [],
}

PROVIDER = {
    "id": 1, "name": "Groq", "key": "groq", "kind": "openai", "enabled": True,
    "base_url": "https://api.groq.com/openai/v1", "prefix": "groq/",
    "priority": 10, "key_count": 1, "model_count": 1, "ok_count": 1,
    "cooling_count": 0, "color": "#f55036", "session": None,
    "enabled_key_count": 1, "last_error": None, "free_tier": "free",
    "docs_url": "https://console.groq.com", "auth_url": "",
    "account_id": "", "format": "openai", "auth_type": "apikey",
}

MODEL = {
    "id": 1, "provider_id": 1, "provider_name": "Groq", "provider_key": "groq",
    "model_id": "llama-3.3-70b-versatile", "exposed_id": "llama-3.3-70b-versatile",
    "display_name": "Llama 3.3 70B", "enabled": True, "status": "healthy",
    "context_length": 128000, "max_output": 8192, "is_free": True,
    "capabilities": {"tools": True, "vision": False, "reasoning": False},
    "pricing_prompt": 0, "pricing_completion": 0,
}

LOG = {
    "id": 1, "ts": 1_700_000_000_000, "client_name": "dashboard",
    "provider_name": "Groq", "model": "auto",
    "upstream_model": "llama-3.3-70b-versatile", "endpoint": "/v1/chat/completions",
    "status": 200, "latency_ms": 12, "tokens_in": 3, "tokens_out": 4,
    "error": None, "via": "upstream", "spoofed": True, "client_key_id": 1,
    "ttft_ms": 5,
}

ROUTE = {
    "id": 1, "public_id": "my-model", "fallbacks": ["llama-3.3-70b-versatile"],
    "auto": True, "enabled": True, "models": ["llama-3.3-70b-versatile"],
}

KEY = {
    "id": 1, "provider_id": 1, "provider_name": "Groq", "label": "sign-in",
    "api_key_preview": "gsk_...abcd", "enabled": True, "weight": 1,
    "cooldown_until": None, "last_error": None, "req_count": 2, "err_count": 0,
    "success_count": 2, "last_used_at": None, "created_at": 1_700_000_000_000,
}

CLIENT_KEY = {
    "id": 1, "name": "dashboard", "token": "nova-sk-...abcd", "prefix": "nova-sk-",
    "enabled": True, "allowed_models": "", "rpm_limit": 0, "tpd_limit": 0,
    "req_count": 2, "tokens_in": 10, "tokens_out": 12, "last_used_at": None,
    "created_at": 1_700_000_000_000,
}


def _payload(path: str) -> Any:
    """What the real JSON API returns for a fresh install with one of each row."""
    if path == "/api/admin/stats":
        return dict(STATS)
    if path == "/api/admin/meta":
        return dict(META)
    if path == "/api/admin/providers":
        return [dict(PROVIDER)]
    if path == "/api/admin/providers/presets":
        return list(META["presets"])
    if path == "/api/admin/models":
        return {"rows": [dict(MODEL)], "total": 1}
    if path.startswith("/api/admin/models/"):
        return dict(MODEL)
    if path == "/api/admin/logs":
        return [dict(LOG)]
    if path == "/api/admin/routes":
        return [dict(ROUTE)]
    if path == "/api/admin/routes/preview":
        return {"model": "auto", "direct": None, "chain": [], "auto": [dict(MODEL)],
                "excluded": [], "strategy": "priority"}
    if path == "/api/admin/keys":
        return [dict(KEY)]
    if path == "/api/admin/client-keys":
        return [dict(CLIENT_KEY)]
    if path in ("/api/agent/tools",):
        return [{"id": "web_search", "name": "Web search", "description": "Search the web",
                 "icon": "globe"}]
    if path == "/api/admin/mcp/tools":
        return {"tools": [], "summary": {"servers": 0, "enabled": 0, "tools": 0, "connected": 0}}
    if path == "/api/admin/mcp/servers":
        return {"servers": [], "summary": {"servers": 0, "enabled": 0, "tools": 0, "connected": 0}}
    if path == "/api/admin/analytics":
        return {"series": [], "groups": [], "totals": {"requests": 4, "errors": 0},
                "p95_latency_ms": 20, "ttft_p95_ms": 8, "ttft_p50_ms": 4, "p50_latency_ms": 12}
    if path == "/api/admin/storage/info":
        return {"providers": [], "files": [], "active": None,
                "usage": {"used_bytes": 0, "total_bytes": 0, "total_mb": 0, "files": 0},
                "providers_catalogue": []}
    if path == "/api/admin/storage/providers":
        return []
    if path == "/api/admin/terminal/history":
        return {"sessions": {"sessions": [], "active": None, "max_sessions": 8}}
    if path == "/api/build/info":
        return {"ok": True, "tools": [], "providers": [], "identity": {}, "engine": {},
                "host": "localhost", "root": "/app/build"}
    if path == "/v1/models":
        return {"object": "list", "data": [{"id": "auto", "owned_by": "routerx"},
                                           {"id": "llama-3.3-70b-versatile", "owned_by": "groq"}]}
    if path == "/api/admin/routing":
        return {"strategy": "priority", "policies": [], "chains": {}, "denylist": [],
                "allowlist": [], "profiles": []}
    if path == "/api/admin/usage":
        return {"usage": [], "windows": [], "cooldowns": []}
    if path == "/api/admin/rate-limits":
        return {"rate_limits": {}, "defaults": {"rpm": 0, "tpm": 0, "rpd": 0, "tpd": 0}}
    if path == "/api/admin/sessions":
        return {"sessions": []}
    if path == "/api/admin/cache/stats":
        return {"enabled": False, "entries": 0, "hits": 0, "misses": 0, "ttl": 300}
    if path == "/api/admin/compute/gpu":
        return {"pools": [], "strategy": "quota_aware"}
    return []


class FakeApi:
    """Stand-in for `ui.api_client.api`: in-process, typed, logged."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: dict[str, Any] = {}

    async def request(self, method: str, path: str, *, json=None, params=None, **_kw):
        base = path.split("?", 1)[0]
        self.calls.append((method, base))
        if base in self.responses:
            value = self.responses[base]
            return value(method, base, json) if callable(value) else value
        return _payload(base)

    async def get(self, path: str, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw):
        return await self.request("POST", path, **kw)

    async def patch(self, path: str, **kw):
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw):
        return await self.request("DELETE", path, **kw)


class QueryParams:
    """`request.query_params.get(key, default)` — Starlette's mutable default."""

    def __init__(self, params) -> None:
        self._params = params

    def get(self, key, default=None):
        return self._params.get(key, default)

    def __getitem__(self, key):
        return self._params[key]

    def __contains__(self, key):
        return key in self._params

    def items(self):
        return list(self._params.items())


class FakeRequest:
    """The query/header subset of a Starlette Request the UI endpoints read."""

    def __init__(self, url: str = "/", headers: dict[str, str] | None = None) -> None:
        import httpx

        self.url = httpx.URL("http://127.0.0.1" + url)
        self.query_params = QueryParams(self.url.params)
        self.headers = headers or {}
        self.base_url = httpx.URL("http://127.0.0.1")
        self.state = type("State", (), {})()

    async def json(self):
        return {}


class FakeForm(dict):
    """A form mapping with the methods `ui.render.form_dict` uses."""

    def items(self):  # type: ignore[override]
        return dict.items(self)

    def multi_items(self):
        return list(dict.items(self))


class FormRequest(FakeRequest):
    def __init__(self, form: dict[str, str]) -> None:
        super().__init__("/")
        self._form = FakeForm(form)

    async def form(self):
        return self._form


def _walk(router):
    """Flatten nested routers (FastAPI stores an included router as a node)."""
    for node in router.routes:
        if hasattr(node, "path"):
            yield node
        nested = getattr(node, "original_router", None)
        if nested is not None:
            yield from _walk(nested)


def _ui_routes() -> dict[str, Any]:
    out = {}
    for route in _walk(ui_router):
        for method in getattr(route, "methods", ()) or ():
            out[f"{method} {route.path}"] = route
    return out


SHELL = TEMPLATES / "base.html"
# `{% set groups = [...] %}` declares one tuple per sidebar entry:
#     ('key', 'Label', 'icon', 'subtitle')
_GROUPS_BLOCK = re.compile(r"\{%\s*set\s+groups\s*=\s*\[(.*?)\]\s*%\}", re.S)
_NAV_ITEM = re.compile(r"\(\s*'([a-z0-9_-]+)'\s*,\s*'([^']*)'\s*,")


def _sidebar_items(shell_text: str | None = None) -> list[tuple[str, str]]:
    """The sidebar entries declared by `templates/base.html`, in order.

    The source of truth, not a hand-maintained list: adding or removing a nav
    item in the template changes this automatically.
    """
    text = SHELL.read_text(encoding="utf-8") if shell_text is None else shell_text
    block = _GROUPS_BLOCK.search(text)
    if block is None:
        raise AssertionError("templates/base.html has no `{% set groups = [...] %}` sidebar block")
    items = _NAV_ITEM.findall(block.group(1))
    if not items:
        raise AssertionError("the sidebar `groups` block declares no entries")
    return items


def _args(route, request, form: dict[str, str] | None = None) -> list[Any]:
    """Positional args for a UI endpoint.

    Query parameters keep their defaults here (httpx is not running), except
    a form POST, whose fields are positional in every UI action endpoint.
    """
    import inspect

    args: list[Any] = []
    for name, param in inspect.signature(route.endpoint).parameters.items():
        if name in ("request", "req"):
            args.append(request)
            continue
        if name in ("body", "payload", "form") and param.default is inspect.Parameter.empty:
            args.append({})
            continue
        # Every remaining parameter is a query parameter (httpx is not running,
        # so the URL is the only source) or a form field on a POST.
        if form is not None and name in form:
            value: Any = form[name]
        elif name in getattr(request, "query_params", {}):
            value = request.query_params.get(name)
        else:
            continue  # keep the endpoint's own default (declared after this one)
        try:
            value = param.annotation(value)
        except Exception:  # noqa: BLE001 - non-str annotations are rare here
            pass
        args.append(value)
    return args


async def render_route(route, request, form: dict[str, str] | None = None) -> str:
    args = _args(route, request, form)
    result = await route.endpoint(*args)
    if isinstance(result, str):
        return result
    body = getattr(result, "body", b"")
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return json.dumps(result, default=str)


class UiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each test renders every tab twice; the Overview keeps a KPI delta
        # snapshot between calls, which must not leak across tests.
        import ui.tabs.overview as overview

        overview._prev_stats = None
        overview._prev_at = 0.0
        self.fake = FakeApi()
        self._swapped = [api_client]
        self._original = {api_client: api_client.api}
        api_client.api = self.fake  # type: ignore[assignment]
        for name in _TAB_MODULES:
            module = importlib.import_module(name)
            if hasattr(module, "api"):
                self._original[module] = module.api
                module.api = self.fake
                self._swapped.append(module)

    def tearDown(self) -> None:
        for module, original in self._original.items():
            module.api = original

    def get(self, path: str, headers: dict[str, str] | None = None) -> str:
        routes = _ui_routes()
        base = path.split("?", 1)[0]
        route = routes.get(f"GET {base}")
        self.assertIsNotNone(route, f"no GET route for {base}")
        return asyncio.run(render_route(route, FakeRequest(path, headers)))

    def post_form(self, path: str, form: dict[str, str]) -> str:
        route = _ui_routes().get(f"POST {path}")
        self.assertIsNotNone(route, f"no POST route for {path}")
        return asyncio.run(render_route(route, FormRequest(form), form))

    def assertRendered(self, path: str, body: str, *, allow_empty: bool = False) -> None:
        self.assertNotIn("{{", body, f"{path} leaked an unrendered template")
        self.assertNotIn("{%", body, f"{path} leaked template statements")
        self.assertNotIn("jinja2.exceptions", body, f"{path} rendered a template error")
        if not allow_empty:
            self.assertTrue(body.strip(), f"{path} rendered nothing")

    # ------------------------------------------------------------------ tabs
    def test_registry_covers_every_tab_module(self):
        registered = {
            r.path.rsplit("/", 1)[-1]
            for r in _walk(ui_router)
            if r.path.startswith("/partials/tab/")
        }
        self.assertEqual(registered, set(TAB_KEYS))

    def test_every_tab_renders_with_an_empty_database(self):
        for key in TAB_KEYS:
            with self.subTest(tab=key):
                body = self.get(f"/partials/tab/{key}")
                self.assertRendered(f"/partials/tab/{key}", body)

    def test_every_tab_renders_with_one_of_every_row(self):
        for key in TAB_KEYS:
            with self.subTest(tab=key):
                body = self.get(f"/partials/tab/{key}")
                self.assertRendered(f"/partials/tab/{key}", body)
        called = {path for _method, path in self.fake.calls}
        self.assertIn("/api/admin/models", called, "fragments must read the app's own JSON API")
        self.assertIn("/api/admin/logs", called)
        self.assertIn("/v1/models", called, "the Playground picker reads the gateway catalogue")

    def test_sub_fragments_render(self):
        candidates = (
            "/partials/footer-stats",
            "/partials/providers/list",
            "/partials/providers/list?region=results&q=zzz",
            "/partials/providers/keys?provider_id=1",
            "/partials/models/list",
            "/partials/models/list?region=results",
            "/partials/models/detail?id=1",
            "/partials/routes/models",
            "/partials/routes/preview?model=auto",
            "/ui/console/plugins",
            "/ui/console/plugins/form",
            "/ui/console/plugins/run?server=x&tool=y",
            "/ui/terminal/providers",
            "/ui/agent/panel",
        )
        routes = _ui_routes()
        checked = 0
        for path in candidates:
            base = path.split("?", 1)[0]
            if f"GET {base}" not in routes:
                continue
            with self.subTest(path=path):
                self.assertRendered(path, self.get(path), allow_empty=True)
                checked += 1
        self.assertGreater(checked, 6)

    # --------------------------------------------------------------- actions
    def test_playground_send_renders_nova_chips_and_escapes(self):
        def gateway(method, path, payload):
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(payload["model"], "auto")
            return {
                "id": "chatcmpl-test", "object": "chat.completion",
                "model": payload["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "hi <b>there</b>"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "_nova": {"provider": "groq", "upstream_model": "llama", "stage": 2,
                          "fallback": True, "spoofed": True, "cached": False},
            }

        self.fake.responses["/v1/chat/completions"] = gateway
        body = self.post_form("/ui/playground/send",
                              {"model": "auto", "message": "hello", "request_id": "abc123"})
        self.assertRendered("playground send", body)
        self.assertIn("hello", body)            # the user bubble
        self.assertIn("groq", body)             # provider chip from _nova
        self.assertIn("fallback", body)
        self.assertIn("&lt;b&gt;", body)        # answers are escaped
        self.assertNotIn("<b>there</b>", body)

    def test_playground_send_reports_a_gateway_error_inline(self):
        def boom(method, path, payload):
            raise api_client.ApiError("Invalid or disabled API key.", 401)

        self.fake.responses["/v1/chat/completions"] = boom
        body = self.post_form("/ui/playground/send", {"model": "auto", "message": "hello"})
        self.assertRendered("playground error", body)
        self.assertIn("HTTP 401", body)
        self.assertIn("Invalid or disabled API key.", body)

    def test_playground_send_rejects_an_empty_message(self):
        body = self.post_form("/ui/playground/send", {"message": "   "})
        self.assertIn("Type a message first", body)
        self.assertNotIn(("/v1/chat/completions", "GET"), self.fake.calls)

    def test_playground_meta_matches_by_request_id(self):
        self.fake.responses["/api/admin/logs"] = [
            {"id": 9, "request_id": "abc123", "model": "auto",
             "upstream_model": "llama", "provider_name": "groq", "via": "upstream",
             "spoofed": True, "latency_ms": 10, "tokens_in": 1, "tokens_out": 2},
            {"id": 8, "request_id": "other", "model": "auto",
             "upstream_model": "x", "provider_name": "x", "via": "upstream"},
        ]
        body = json.loads(self.get("/ui/playground/meta?request_id=abc123"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["nova"]["provider"], "groq")
        self.assertTrue(body["nova"]["fallback"])
        missing = json.loads(self.get("/ui/playground/meta?request_id=nope"))
        self.assertFalse(missing["ok"])

    def test_playground_meta_needs_no_console_chat_meta(self):
        """The streamed chips path must not depend on the timestamp guesser."""
        source = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("/ui/console/chat-meta?since=", source)
        self.assertIn("/ui/playground/meta?request_id=", source)

    # ----------------------------------------------------------------- shell
    def test_shell_renders_every_sidebar_entry(self):
        route = next(r for r in shell_router.routes if getattr(r, "path", "") == "/")
        body = asyncio.run(route.endpoint()).body.decode("utf-8")
        self.assertRendered("/", body)
        # The sidebar has 10 entries, declared once in `templates/base.html`
        # (`{% set groups = [...] %}`). `terminal` opens the standalone cloud
        # workspace at /terminal in a new browser tab (static/app.js); the
        # other nine swap dashboard fragments.
        items = _sidebar_items()
        expected = [key for key, _label in items]
        self.assertEqual(len(items), 10, f"sidebar entries changed: {expected}")
        self.assertEqual(len(set(expected)), len(expected), f"duplicate sidebar keys: {expected}")
        rendered = set(re.findall(r'data-tab="([a-z0-9_-]+)"', body))
        for key, label in items:
            self.assertIn(f'data-tab="{key}"', body, f"sidebar is missing {key}")
            self.assertIn(f'data-label="{label}"', body, f"sidebar is missing the {label} label")
        # Reverse: every nav item in the shell is declared by the template.
        self.assertEqual(rendered - set(expected), set(),
                         "rendered a sidebar entry that templates/base.html does not declare")
        # `terminal` navigates away instead of swapping a fragment, so it is
        # the easiest entry to lose in a retheme -- pin it explicitly.
        self.assertIn('data-tab="terminal"', body)
        self.assertIn("RouterX Console", body)
        self.assertRendered("/terminal", self.get("/terminal"))
        self.assertIn("tailwind.config", body)
        # The backdrop must never sit above content or eat pointer events.
        self.assertIn('class="nx-world"', body)

    def test_no_duplicate_ids_in_rendered_tabs(self):
        pattern = re.compile(r'\bid="([^"]+)"')
        for key in TAB_KEYS:
            with self.subTest(tab=key):
                ids = pattern.findall(self.get(f"/partials/tab/{key}"))
                dupes = sorted({i for i in ids if ids.count(i) > 1})
                self.assertEqual(dupes, [], f"duplicate ids in {key}")


class TemplateHygieneTests(unittest.TestCase):
    def test_every_template_parses(self):
        errors = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            name = path.relative_to(TEMPLATES).as_posix()
            try:
                env.get_template(name)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{name}: {err.__class__.__name__}: {err}")
        self.assertEqual(errors, [])

    def test_icon_only_buttons_have_an_accessible_name(self):
        """A button whose whole content is an icon needs aria-label or real text."""
        pattern = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S | re.I)
        offenders = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            src = path.read_text(encoding="utf-8")
            for match in pattern.finditer(src):
                attrs, inner = match.group(1), match.group(2)
                text = re.sub(r"<[^>]+>", "", inner)
                # Jinja output is text; Jinja statements are not.
                text = re.sub(r"\{\{.*?\}\}", "TEXT", text, flags=re.S)
                text = re.sub(r"\{%.*?%\}", "", text, flags=re.S).strip()
                if text or "aria-label" in attrs:
                    continue
                if "<i " in inner:
                    line = src[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(TEMPLATES)}:{line}")
        self.assertEqual(offenders, [])

    def test_reduced_motion_is_honoured_by_the_world_layer(self):
        css = (Path(__file__).resolve().parent.parent / "static" / "nova-world.css").read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("z-index: -1", css)
        self.assertIn("pointer-events: none", css)

    def test_tailwind_colour_ramps_used_by_templates_are_defined(self):
        """A remapped ramp the CDN config dropped renders nothing (invisible)."""
        shell = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        configured = set(re.findall(r"^\s{12}(\w+): \{", shell, re.M))
        self.assertIn("emerald", configured)
        self.assertIn("slate", configured)
        self.assertIn("rose", configured)
        # amber/green/purple are used by semantic utilities and are NOT
        # remapped: renaming or dropping them must be a deliberate decision.
        for required in ("amber", "green", "purple"):
            self.assertIn(required, shell, f"{required}-* is used by templates")
            self.assertNotIn(f"            {required}: {{", shell,
                             f"{required}-* must stay the stock ramp or every use changes meaning")


if __name__ == "__main__":
    unittest.main()
