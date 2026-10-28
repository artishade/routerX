"""Tab registry — each dashboard tab is its own Python module + templates.

Tabs (mirroring the React original): overview, playground, console, providers,
models, routes, keys, storage, analytics, logs, terminal. Every tab module
defines:
  • router: APIRouter with GET /partials/tab/<key> (the main fragment)
  • optional /ui/<key>/... action endpoints returning HTML fragments
"""
from __future__ import annotations

from fastapi.routing import APIRouter

from ui.tabs import (  # noqa: F401  (side-effect free, each exports `router`)
    analytics,
    console,
    keys,
    logs,
    models,
    overview,
    playground,
    providers,
    routes,
    storage,
    terminal,
)

router = APIRouter(tags=["ui:tabs"])
for _module in (overview, playground, console, providers, models, routes, keys, storage, analytics, logs, terminal):
    router.include_router(_module.router)

ui_router = APIRouter(tags=["ui"])
ui_router.include_router(router)
