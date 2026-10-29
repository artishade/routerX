"""Keys tab — Python UI port of src/components/nova/tabs/KeysTab.tsx.

Client access keys: issue (mint) dialog, show-token-once reveal, edit limits,
enable/disable toggle, revoke with confirm, usage stats + daily token budget
bar, and the gateway usage hint.

Fragment endpoints:
  GET  /partials/tab/keys      → tab frame (dialogs + HTMX loader for the body)
  GET  /partials/keys/main     → header + key cards + usage hint (HTMX, reloads
                                 on `nova:refresh` — no data-live here, the keys
                                 tab never polls on its own)
Action endpoints (BFF → /api/admin/client-keys):
  POST /ui/keys/create          → JSON {ok, key} (token returned ONCE)
  PATCH /ui/keys/{id}           → JSON {ok} (edit limits dialog)
  POST /ui/keys/{id}/toggle     → HTMX card swap + toast (enable/disable)
  POST /ui/keys/{id}/delete     → HTMX card swap + toast (revoke, hx-confirm)
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ui.api_client import ApiError, api
from ui.render import form_dict, html, render, truthy

router = APIRouter(tags=["ui:keys"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _num_field(value: Any, label: str) -> int | None:
    """TSX `Number(x)` parity for form strings: '' → 0, non-finite/negative → None."""
    text = str(value if value is not None else "").strip()
    if text == "":
        return 0
    try:
        n = float(text)
    except ValueError:
        return None
    if not math.isfinite(n) or n < 0:
        return None
    return max(0, round(n))


def _err_panel(message: str) -> HTMLResponse:
    return html(
        render("partials/keys_error.html", message=message, retry_url="/partials/keys/main"),
        status_code=200,
    )


async def _fetch_key(key_id: int) -> dict | None:
    """Fresh single key (there is no single-key GET on the JSON API)."""
    try:
        rows = await api.get("/api/admin/client-keys")
    except (ApiError, Exception):
        return None
    for k in rows or []:
        if isinstance(k, dict) and k.get("id") == key_id:
            return k
    return None


# --------------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------------- #

@router.get("/partials/tab/keys")
async def keys_tab() -> HTMLResponse:
    """Tab frame — dialogs + loader; the body itself loads via /partials/keys/main."""
    return html(render("tabs/keys.html"))


@router.get("/partials/keys/main")
async def keys_main() -> HTMLResponse:
    """Header + cards + hint (mirrors KeysTab body)."""
    keys, stats, meta = await asyncio.gather(
        api.get("/api/admin/client-keys"), api.get("/api/admin/stats"),
        api.get("/api/admin/meta"), return_exceptions=True,
    )
    if isinstance(keys, BaseException):
        message = keys.message if isinstance(keys, ApiError) else str(keys) or "Failed to load client keys"
        return _err_panel(message)

    if isinstance(stats, BaseException):
        stats = None
    if isinstance(meta, BaseException):
        meta = None

    return html(render("partials/keys_main.html", keys=keys or [], stats=stats, meta=meta))


# --------------------------------------------------------------------------- #
# POST /ui/keys/create — mint a client key (JSON; JS opens the show-once dialog)
# --------------------------------------------------------------------------- #

@router.post("/ui/keys/create")
async def keys_create(request: Request) -> JSONResponse:
    form = await form_dict(request)

    name = str(form.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "Give the key a name so you can recognise it later"}, status_code=400)

    rpm = _num_field(form.get("rpm_limit"), "rpm")
    if rpm is None:
        return JSONResponse({"error": "RPM limit must be a non-negative number"}, status_code=400)
    tpd = _num_field(form.get("tpd_limit"), "tpd")
    if tpd is None:
        return JSONResponse({"error": "TPD limit must be a non-negative number (0 = unlimited)"}, status_code=400)

    allowed = str(form.get("allowed_models") or "").strip() or "*"
    scopes = str(form.get("trading_scopes") or "").strip()

    try:
        created = await api.post(
            "/api/admin/client-keys",
            json={"name": name, "allowed_models": allowed, "trading_scopes": scopes, "rpm_limit": rpm, "tpd_limit": tpd},
        )
    except ApiError as err:
        return JSONResponse({"error": err.message}, status_code=err.status if err.status >= 400 else 500)

    return JSONResponse({"ok": True, "key": created})


# --------------------------------------------------------------------------- #
# PATCH /ui/keys/{id} — edit limits (JSON; JS consumes from the edit dialog)
# --------------------------------------------------------------------------- #

@router.patch("/ui/keys/{key_id}")
async def keys_update(key_id: str, request: Request) -> JSONResponse:
    try:
        kid = int(key_id)
    except ValueError:
        return JSONResponse({"error": "Invalid client key id"}, status_code=400)

    form = await form_dict(request)
    body: dict[str, Any] = {}

    name = str(form.get("name") or "").strip()
    if name:
        body["name"] = name
    allowed = str(form.get("allowed_models") or "").strip()
    if allowed:
        body["allowed_models"] = allowed
    if "trading_scopes" in form:
        body["trading_scopes"] = str(form.get("trading_scopes") or "").strip()
    rpm = _num_field(form.get("rpm_limit"), "rpm")
    if rpm is not None:
        body["rpm_limit"] = rpm
    tpd = _num_field(form.get("tpd_limit"), "tpd")
    if tpd is not None:
        body["tpd_limit"] = tpd

    try:
        await api.patch(f"/api/admin/client-keys/{kid}", json=body)
    except ApiError as err:
        return JSONResponse({"error": err.message}, status_code=err.status if err.status >= 400 else 500)

    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# POST /ui/keys/{id}/toggle — enable/disable (HTMX; returns the fresh card)
# --------------------------------------------------------------------------- #

@router.post("/ui/keys/{key_id}/toggle")
async def keys_toggle(key_id: str, request: Request) -> HTMLResponse:
    form = await form_dict(request)
    enabled = truthy(form.get("enabled"))

    try:
        kid = int(key_id)
    except ValueError:
        return html("", toast={"message": "Invalid client key id", "type": "error"}, refresh=True)

    try:
        await api.patch(f"/api/admin/client-keys/{kid}", json={"enabled": enabled})
    except ApiError as err:
        key = await _fetch_key(kid)
        if key is None:
            return html("", toast={"message": err.message, "type": "error"}, refresh=True)
        return html(
            render("partials/keys_card.html", k=key),
            toast={"message": err.message, "type": "error"},
        )

    key = await _fetch_key(kid)
    if key is None:
        return html("", toast={"message": "Key updated", "type": "success"}, refresh=True)

    label = "enabled" if enabled else "disabled"
    return html(
        render("partials/keys_card.html", k=key),
        toast={"message": f"Key \"{key.get('name', '')}\" {label}", "type": "success"},
        refresh=True,
    )


# --------------------------------------------------------------------------- #
# POST /ui/keys/{id}/delete — revoke (HTMX; hx-confirm gate in the card)
# --------------------------------------------------------------------------- #

@router.post("/ui/keys/{key_id}/delete")
async def keys_delete(key_id: str) -> HTMLResponse:
    try:
        kid = int(key_id)
    except ValueError:
        return html("", toast={"message": "Invalid client key id", "type": "error"}, refresh=True)

    key = await _fetch_key(kid)
    name = (key or {}).get("name", "")

    try:
        await api.delete(f"/api/admin/client-keys/{kid}")
    except ApiError as err:
        return html("", toast={"message": err.message, "type": "error"}, refresh=True)

    return html(
        "",
        toast={"message": f"Key \"{name}\" revoked", "type": "success"},
        refresh=True,
    )
