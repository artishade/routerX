"""End-to-end smoke test for the RouterX gateway (no live server needed).

Boots the real FastAPI app in-process (lifespan included: schema bootstrap +
RouterX Free engine sidecar), then exercises the paths Claude Code and OpenAI SDKs
use. Run it with the project venv:

    python3 tests/smoke_gateway.py

It fails loudly on contract violations and reports (without failing) when a
free upstream is unreachable from the current network — the gateway must always
answer with a well-formed payload, never a fabricated one.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []
notes: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    import main as app_module

    app = app_module.app
    print("· booting app (lifespan: bootstrap + engine sidecar)")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://nova.test", timeout=120.0) as client:
            # ---- /health -------------------------------------------------
            print("· GET /health")
            res = await client.get("/health")
            body = res.json()
            check(res.status_code == 200, "/health is 200", str(res.status_code))
            check(body.get("ok") is True, "/health ok=true", json.dumps(body))
            check(body.get("engine") == "up", "/health engine=up (sidecar spawned)", str(body.get("engine")))

            # ---- /v1/models ----------------------------------------------
            print("· GET /v1/models")
            res = await client.get("/v1/models")
            models = (res.json() or {}).get("data") or []
            ids = {m.get("id") for m in models}
            check(res.status_code == 200, "/v1/models is 200", str(res.status_code))
            for tier in ("routerx/mini", "routerx/air", "routerx/pro"):
                check(tier in ids, f"/v1/models exposes {tier}")
            free_ids = [i for i in ids if i and ":" in str(i)]
            check(len(free_ids) >= 10, "/v1/models exposes the RouterX Free catalogue", f"{len(free_ids)} ids")

            # ---- engine catalogue surface --------------------------------
            print("· engine /models (sidecar)")
            res = await client.get("/v1/models")
            check(res.status_code == 200, "catalogue fetch stable", str(res.status_code))

            # ---- /v1/chat/completions (OpenAI shape) ---------------------
            print("· POST /v1/chat/completions (model=routerx/air, non-stream)")
            res = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "routerx/air",
                    "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
                    "max_tokens": 32,
                },
            )
            payload = res.json()
            choice = (payload.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            if res.status_code == 200 and content.strip():
                check(payload.get("model") == "routerx/air", "response model = requested id (spoofing)")
                nova = payload.get("_nova") or {}
                check(bool(nova.get("upstream_model")), f"_nova.upstream_model set ({nova.get('upstream_model')})")
                check(bool(nova.get("provider")), f"_nova.provider set ({nova.get('provider')})")
                print(f"       answer: {content.strip()[:80]!r}")
            else:
                notes.append(
                    f"/v1/chat/completions: no free upstream answered here "
                    f"(status={res.status_code}, body={str(payload)[:200]}) — expected on restricted networks"
                )

            # ---- /v1/messages/count_tokens -------------------------------
            print("· POST /v1/messages/count_tokens")
            res = await client.post(
                "/v1/messages/count_tokens",
                json={
                    "model": "routerx/air",
                    "messages": [{"role": "user", "content": "hello there"}],
                    "tools": [
                        {
                            "name": "get_weather",
                            "description": "Get weather",
                            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                        }
                    ],
                },
                headers={"anthropic-version": "2023-06-01"},
            )
            ct = res.json()
            check(res.status_code == 200, "count_tokens is 200", str(res.status_code))
            check(isinstance(ct.get("input_tokens"), int) and ct["input_tokens"] > 0, "count_tokens.input_tokens > 0", json.dumps(ct))

            # ---- /v1/messages streaming (Claude Code path) --------------
            print("· POST /v1/messages (stream) — Anthropic SSE sequence")
            events: list[tuple[str, dict]] = []
            async with client.stream(
                "POST",
                "/v1/messages",
                json={
                    "model": "routerx/air",
                    "max_tokens": 64,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Say hi in one short word."}],
                    "tools": [
                        {
                            "name": "get_weather",
                            "description": "Get weather",
                            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                        }
                    ],
                },
                headers={"anthropic-version": "2023-06-01"},
            ) as res:
                check(res.status_code == 200, "streaming /v1/messages is 200", str(res.status_code))
                current = None
                async for line in res.aiter_lines():
                    if line.startswith("event: "):
                        current = line[7:].strip()
                    elif line.startswith("data: ") and current:
                        events.append((current, json.loads(line[6:])))
                        if current == "message_stop":
                            break

            kinds = [k for k, _ in events]
            check("message_start" in kinds, "stream has message_start")
            check("content_block_start" in kinds, "stream has content_block_start (the Claude Code bug fix)")
            check("content_block_stop" in kinds, "stream has content_block_stop")
            check("message_delta" in kinds, "stream has message_delta")
            check("message_stop" in kinds, "stream has message_stop")

            if "content_block_start" in kinds and "content_block_delta" in kinds:
                first_block = kinds.index("content_block_start")
                first_delta = kinds.index("content_block_delta")
                check(
                    first_block < first_delta,
                    "content_block_start arrives BEFORE any delta",
                    f"start@{first_block} delta@{first_delta}",
                )
                payload = events[first_block][1]
                cb = payload.get("content_block") or {}
                check(payload.get("index") == 0, "content_block_start index 0", json.dumps(payload)[:120])
                check(cb.get("type") == "text", "content_block_start opens a text block", json.dumps(cb)[:120])

            start_ev = next((d for k, d in events if k == "message_start"), {})
            check(
                isinstance((start_ev.get("message") or {}).get("usage"), dict),
                "message_start carries usage",
            )
            delta_ev = next((d for k, d in events if k == "message_delta"), {})
            check("stop_reason" in (delta_ev.get("delta") or {}), "message_delta carries stop_reason", json.dumps(delta_ev)[:160])
            if not any(k == "content_block_delta" for k in kinds):
                notes.append("stream carried no text deltas — no free upstream answered here")

            # ---- Anthropic non-streaming ---------------------------------
            print("· POST /v1/messages (non-stream)")
            res = await client.post(
                "/v1/messages",
                json={
                    "model": "routerx/air",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
                },
                headers={"anthropic-version": "2023-06-01"},
            )
            msg = res.json()
            if res.status_code == 200:
                blocks = msg.get("content") or []
                check(msg.get("type") == "message", "/v1/messages type=message", str(msg.get("type")))
                check(isinstance(blocks, list) and bool(blocks), "/v1/messages returns content blocks", json.dumps(msg)[:200])
                check(bool(msg.get("stop_reason")), "/v1/messages has stop_reason", str(msg.get("stop_reason")))
            else:
                notes.append(f"/v1/messages non-stream: no free upstream (status={res.status_code})")

            # ---- dashboard shell -----------------------------------------
            print("· GET / (dashboard shell)")
            res = await client.get("/")
            check(res.status_code == 200 and "text/html" in res.headers.get("content-type", ""), "dashboard shell renders HTML", str(res.status_code))

    print()
    for n in notes:
        print(f"note: {n}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
