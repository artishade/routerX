"""Self-API client for the Python UI layer.

The UI is a Backend-for-Frontend: fragment endpoints render HTML by consuming
the very same JSON API that external clients use (/api/admin/*, /api/agent/*,
/v1/*). This guarantees exact behavioral parity with the gateway (sync on
create, probing, masking, aggregation) with zero duplicated logic.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from nova.config import PORT, UI_GATEWAY_TOKEN

log = logging.getLogger("nova.ui.api")

# The dashboard is a BFF over the gateway's own `/v1/*` surface. Once any
# ClientKey exists (or NOVA_REQUIRE_API_KEY is set) `nova.auth.gateway_auth`
# refuses every anonymous call with 401, including the loopback calls this
# client makes — so those calls must present a credential of their own.
# `NOVA_UI_GATEWAY_TOKEN` holds one client key for exactly that. Unset (fresh
# install, no keys) keeps the calls anonymous, which is still allowed.
GATEWAY_TOKEN_ENV = "NOVA_UI_GATEWAY_TOKEN"
_warned: set[str] = set()


def _configured_token() -> str:
    """Env wins (live changes apply on the next client); config is the default."""
    configured = (os.environ.get(GATEWAY_TOKEN_ENV) or "").strip()
    return configured or UI_GATEWAY_TOKEN


class ApiError(Exception):
    """Raised when the JSON API answers with an error — `.message` is user-facing."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.message = message
        self.status = status


def gateway_token() -> str:
    """The client key the dashboard presents to its own gateway ('' = none)."""
    return _configured_token()


class NovaApiClient:
    """Async httpx client pointed at this very server (127.0.0.1:$PORT)."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # read=150: provider tests can legitimately take ~25s (engine cap).
            # The token rides on the client headers so every request (including
            # a meta/stats read) is authorised without each caller knowing.
            headers = {}
            token = gateway_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{PORT}",
                timeout=httpx.Timeout(connect=10.0, read=150.0, write=60.0, pool=120.0),
                headers=headers,
                trust_env=False,
            )
        return self._client

    def _warn_unauthorised(self) -> None:
        """Explain the one deployment knob that fixes a dashboard 401 (once)."""
        if GATEWAY_TOKEN_ENV in _warned:
            return
        _warned.add(GATEWAY_TOKEN_ENV)
        log.warning(
            "the dashboard got a 401 from its own gateway: set %s to an enabled "
            "client key so the BFF can call /v1/* once client-key auth is on",
            GATEWAY_TOKEN_ENV,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if files is not None:
            kwargs["files"] = files
        if data is not None:
            kwargs["data"] = data
        if timeout is not None:
            kwargs["timeout"] = timeout
        elif files is not None or data is not None:
            # Every call has a timeout. An upload is bounded separately: the
            # default read window (150s) is sized for provider tests, not for a
            # response that only starts after the body has been streamed.
            kwargs["timeout"] = httpx.Timeout(connect=10.0, read=60.0, write=300.0, pool=120.0)
        try:
            res = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise ApiError(f"API request failed: {err.__class__.__name__}") from err
        if res.status_code == 401:
            self._warn_unauthorised()
        if res.status_code >= 400:
            try:
                body = res.json()
            except ValueError:
                body = {}
            raise ApiError(str(body.get("error") or f"HTTP {res.status_code}"), res.status_code)
        if res.status_code == 204 or not res.content:
            return None
        return res.json()

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def patch(self, path: str, **kw: Any) -> Any:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)


    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

api = NovaApiClient()
