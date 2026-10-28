"""OpenAI Responses API adapter - POST /v1/responses.

Newer OpenAI SDKs and agents speak the Responses shape (`input`, `instructions`,
`output[].content[].text`) instead of chat messages. This module is a thin,
stateless translation onto the existing non-streaming/streaming chat pipeline:
it never re-implements routing, fallback, caps or logging.

Request:  {model, input, instructions?, stream?, max_output_tokens?,
           temperature?, tools?}
          `input` is a string or a Responses-style message array
          (`{role, content}` with string or `input_text`/`output_text` parts).

Response: {id, object: "response", created_at, model, status: "completed",
           output: [{type: "message", role: "assistant",
                     content: [{type: "output_text", text}]}],
           usage: {input_tokens, output_tokens, total_tokens},
           _nova: {...}}

Streaming emits `response.created`, `response.output_text.delta` and
`response.completed`; any failure after the stream opened emits
`response.failed` with the same `error` shape as the rest of the gateway.

It is included by `routers/gateway.py`, so the double mount in `main.py` serves
it under both `/v1` and `/api/v1` with no extra wiring.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from nova import auth as nova_auth
from nova import session as nova_session

router = APIRouter()

ENDPOINT = "/v1/responses"


def _deps():
    """Gateway helpers, resolved lazily (this module is imported at the end of gateway)."""
    from . import gateway as gw

    return gw


# --------------------------------------------------------------------------- #
# Request translation
# --------------------------------------------------------------------------- #

def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    for key in ("text", "input_text", "output_text"):
        value = part.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            return value["value"]
    return ""


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (_part_text(p) for p in content) if t)
    return str(content)


def input_to_messages(payload: dict) -> tuple[list[dict], str | None]:
    """Responses `input` + `instructions` -> chat messages + system text."""
    instructions = payload.get("instructions")
    system_text = instructions.strip() if isinstance(instructions, str) and instructions.strip() else None
    raw = payload.get("input")
    messages: list[dict] = []
    if isinstance(raw, str):
        if raw.strip():
            messages.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                if item.strip():
                    messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type and item_type not in ("message",):
                # function_call / function_call_output etc. have no chat twin here
                continue
            role = str(item.get("role") or "user")
            role = "system" if role in ("system", "developer") else (
                "assistant" if role == "assistant" else "user")
            text = _content_text(item.get("content"))
            if text:
                messages.append({"role": role, "content": text})
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})
    return messages, system_text


def chat_body(payload: dict, messages: list[dict]) -> dict:
    """The chat-shaped body the pipeline/builders expect."""
    gw = _deps()
    body: dict[str, Any] = {"model": payload.get("model"), "messages": messages}
    if isinstance(payload.get("temperature"), (int, float)):
        body["temperature"] = payload["temperature"]
    if isinstance(payload.get("top_p"), (int, float)):
        body["top_p"] = payload["top_p"]
    max_out = payload.get("max_output_tokens")
    if isinstance(max_out, int) and max_out > 0:
        body["max_tokens"] = max_out
    if isinstance(payload.get("tools"), list) and payload["tools"]:
        body["tools"] = payload["tools"]
    if payload.get("tool_choice") is not None:
        body["tool_choice"] = payload["tool_choice"]
    if payload.get("metadata") is not None:
        body["metadata"] = payload["metadata"]
    # Validate the translation once up front so a malformed tool def is a 400,
    # not a stage failure that walks the entire fallback chain.
    gw.build_upstream_body(body, str(payload.get("model") or ""), messages)
    return body


def response_object(requested: str, text: str, prompt_tokens: int, completion_tokens: int,
                    meta: dict | None = None, *, response_id: str | None = None,
                    created_at: int | None = None, status: str = "completed") -> dict:
    """The Responses-shaped body for a completed turn."""
    return {
        "id": response_id or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": created_at or int(time.time()),
        "model": requested,
        "status": status,
        "output": [{
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "_nova": meta or {},
    }


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _unpack_stream(opened) -> tuple[Any, str, str, int | None]:
    """`(iterator, provider_name, upstream_model, key_id)` from an open stream.

    `gateway.attempt_upstream_stream` returns an `UpstreamStream`: a 3-tuple
    (`stream, provider_name, upstream_model`) that also carries `provider_name`,
    `upstream_model` and `key_id` as attributes. Indexing it blindly put the
    KEY ID into `upstream_model` and raised `AttributeError` on `.key_id` for a
    plain tuple, which the old bare `except` turned into a silent "provider
    failed". Read the tuple positions explicitly and take the key id from the
    attribute, falling back to a fourth element when one exists.
    """
    if not isinstance(opened, (tuple, list)) or len(opened) < 3:
        raise TypeError(f"attempt_upstream_stream returned {type(opened).__name__}, expected a 3-tuple")
    stream, provider_name, upstream_model = opened[0], opened[1], opened[2]
    key_id = getattr(opened, "key_id", None)
    if key_id is None and len(opened) > 3:
        key_id = opened[3]
    return stream, provider_name, upstream_model, key_id


# --------------------------------------------------------------------------- #
# POST /v1/responses
# --------------------------------------------------------------------------- #

@router.post("/responses")
async def create_response(request: Request, principal=Depends(nova_auth.gateway_auth)):
    gw = _deps()
    session_id = nova_session.session_id_from(request.headers, None)
    gw.enter_request(session_id=session_id,
                     client_key_id=getattr(principal, "key_id", None) if principal is not None else None)
    started = gw.now_ms()
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)

    requested = str(payload.get("model") or "").strip()
    if not requested:
        return JSONResponse({"error": {"message": "Missing required parameter: 'model'",
                                       "type": "invalid_request_error"}}, status_code=400)
    refusal = gw.enforce_client(principal, requested)
    if refusal is not None:
        return await gw.auth_refusal(refusal, requested, ENDPOINT)

    messages, _system = input_to_messages(payload)
    if not messages:
        return JSONResponse({"error": {"message": "Missing required parameter: 'input'",
                                       "type": "invalid_request_error"}}, status_code=400)

    try:
        body = chat_body(payload, messages)
    except Exception as err:  # noqa: BLE001 - malformed tools are a client error
        return JSONResponse({"error": {"message": f"Invalid request: {err.__class__.__name__}",
                                       "type": "invalid_request_error"}}, status_code=400)

    temperature = body.get("temperature")
    forward_headers = gw.extract_forward_headers(request=request)
    tokens_in = gw.estimate_tokens("\n".join(f"{m['role']}:{m['content']}" for m in messages))
    client_key_id = getattr(principal, "key_id", None) if principal is not None else None

    if payload.get("stream") is True:
        return StreamingResponse(
            _responses_stream(requested, messages, temperature, tokens_in, started, body,
                              forward_headers, client_key_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **gw.build_headers()},
        )

    from nova.database import SessionLocal

    with SessionLocal() as db:
        try:
            result, stage, plan = await gw.pipeline_nonstream(
                db, requested, messages, temperature, body, forward_headers)
        except gw.HTTPError as err:
            gw.write_log(db, model=requested, upstream_model="nova-engine",
                         provider_name="", endpoint=ENDPOINT,
                         status=err.status, latency_ms=gw.now_ms() - started,
                         error=err.message)
            return JSONResponse({"error": {"message": err.message, "type": "gateway_error",
                                           "code": err.status}}, status_code=err.status)
        except Exception as err:  # noqa: BLE001 - never a bare 500
            gw.log.exception("unhandled error in /v1/responses")
            gw.write_log(db, model=requested, upstream_model="nova-engine",
                         provider_name="", endpoint=ENDPOINT, status=502,
                         latency_ms=gw.now_ms() - started,
                         error=f"{err.__class__.__name__}: {err}")
            return JSONResponse({"error": {"message": f"Gateway failed to serve the request ({err.__class__.__name__})",
                                           "type": "gateway_error", "code": 502}}, status_code=502)
        gw.persist_lockouts(db)

        text = gw.strip_think_blocks(result.get("content") or "") or ""
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or tokens_in
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") \
            or gw.estimate_tokens(text)
        latency = gw.now_ms() - started
        meta = gw.nova_meta(result["upstream_model"], result["provider_name"], result["stage"], requested,
                            result["stage"] > 1 or not plan.get("direct", True),
                            gw.routing_summary(plan), handoff=gw.current_handoff())
        meta["latency_ms"] = latency
        gw.write_log(db, model=requested, upstream_model=result["upstream_model"],
                     provider_name=result["provider_name"], endpoint=ENDPOINT, status=200,
                     latency_ms=latency, tokens_in=prompt_tokens, tokens_out=completion_tokens,
                     via=result["via"], spoofed=meta["spoofed"])
        gw.record_served(db, requested, result, messages, prompt_tokens, completion_tokens)
        gw.attribute_client(db, principal, prompt_tokens, completion_tokens)

        return JSONResponse(response_object(requested, text, prompt_tokens, completion_tokens, meta))


async def _responses_stream(requested: str, messages: list[dict], temperature, tokens_in: int,
                            started: int, body: dict, forward_headers: dict[str, str] | None,
                            client_key_id: int | None):
    """SSE: response.created -> response.output_text.delta* -> response.completed."""
    gw = _deps()
    gw.enter_request(session_id=gw.current_session_id(), client_key_id=client_key_id)
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_at = int(time.time())
    assembled: list[str] = []
    served = {"upstream_model": "nova-engine", "provider_name": "RouterX Free Engine",
              "via": "nova-engine", "stage": 0}

    def created_event(status: str = "in_progress") -> bytes:
        return _sse("response.created", {
            "type": "response.created", "sequence_number": 0,
            "response": {"id": response_id, "object": "response", "created_at": created_at,
                         "model": requested, "status": status, "output": []},
        })

    from nova.database import SessionLocal

    db = SessionLocal()
    committed = False
    ttft_ms = 0
    try:
        yield created_event("in_progress")
        try:
            rows, plan = gw.route_request(db, requested, body)
        except gw.HTTPError as err:
            yield _sse("response.failed", {"type": "response.failed", "response_id": response_id,
                                           "error": {"message": err.message, "code": err.status}})
            return
        except Exception as err:  # noqa: BLE001
            gw.log.exception("responses stream routing failed")
            yield _sse("response.failed", {"type": "response.failed", "response_id": response_id,
                                           "error": {"message": f"Gateway routing failed ({err.__class__.__name__})",
                                                     "code": 502}})
            return

        stage = 0
        stages: list[str] = []
        for row in rows:
            stage += 1
            try:
                opened = await gw.attempt_upstream_stream(
                    row, messages, temperature, body, forward_headers=forward_headers)
                stream, provider_name, upstream_model, key_id = _unpack_stream(opened)
            except gw.UpstreamFailure as err:
                detail = f"stage {stage}: {err.detail}"
                stages.append(detail)
                gw.note_attempt(row, False, err.detail)
                await gw.asyncio.to_thread(gw.cooldown_after_failure,
                                           err.provider_id or (row.provider.id if row.provider else None),
                                           err.key_id, err.status, err.retry_after, err.detail)
                continue
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as err:
                # Transport / protocol faults of one stage: fall through quietly.
                detail = f"stage {stage}: {err.__class__.__name__}: {err}"
                stages.append(detail)
                gw.note_attempt(row, False, detail)
                gw.log.info("responses stream %s", detail)
                continue
            except Exception as err:  # noqa: BLE001 - programming errors stay loud
                gw.log.exception("responses stream stage %s raised", stage)
                stages.append(f"stage {stage}: {err.__class__.__name__}: {err}")
                gw.note_attempt(row, False, f"{err.__class__.__name__}: {err}")
                continue
            pieces = gw._iterate_with_heartbeat(stream, b": routerx-keepalive\n\n")
            first = None
            async for kind, value in pieces:
                if kind == "hb":
                    yield value
                    continue
                if kind == "piece" and (value.get("text") or value.get("finish_reason")
                                        or value.get("tool_calls")):
                    first = value
                break
            if first is None:
                await gw._close_stream(stream)
                stages.append(f"stage {stage}: empty stream")
                continue
            committed = True
            ttft_ms = gw.now_ms() - started
            gw.note_attempt(row, True)
            # The key that actually opened the winning stream is the one booked.
            await gw.asyncio.to_thread(gw.note_key_success, key_id)
            served = {"upstream_model": upstream_model, "provider_name": provider_name,
                      "provider_id": row.provider.id if row.provider is not None else None,
                      "upstream_exposed_id": row.exposedId, "key_id": key_id,
                      "via": "upstream", "stage": stage}
            if first.get("text"):
                text = gw.strip_think_blocks(first["text"]) or ""
                if text:
                    assembled.append(text)
                    yield _sse("response.output_text.delta",
                               {"type": "response.output_text.delta", "response_id": response_id,
                                "output_index": 0, "content_index": 0, "delta": text})
            if not first.get("finish_reason"):
                async for kind, piece in pieces:
                    if kind == "eof":
                        break
                    if kind == "hb":
                        yield piece
                        continue
                    text = piece.get("text") or ""
                    if text:
                        filtered = gw.strip_think_blocks(text) or ""
                        if filtered:
                            assembled.append(filtered)
                            yield _sse("response.output_text.delta",
                                       {"type": "response.output_text.delta", "response_id": response_id,
                                        "output_index": 0, "content_index": 0, "delta": filtered})
                    if piece.get("finish_reason"):
                        break
            break

        if not committed:
            # Final stage: the RouterX Free engine (streamed).
            try:
                iterator = await gw.nova_engine.chat_stream(
                    messages, tools=body.get("tools"), tool_choice=body.get("tool_choice"),
                    model=requested)
                async for data in iterator:
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    engine_meta = chunk.get("_nova")
                    if isinstance(engine_meta, dict) and not committed:
                        served["upstream_model"] = engine_meta.get("upstream_model") or served["upstream_model"]
                        served["provider_name"] = engine_meta.get("provider") or served["provider_name"]
                    if chunk.get("error"):
                        raise gw.nova_engine.EngineUnavailable(str(chunk.get("error")))
                    choices = chunk.get("choices")
                    text = ""
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
                        text = delta.get("content") or ""
                        if not text:
                            message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
                            text = message.get("content") or ""
                    if isinstance(text, str) and text:
                        committed = True
                        if not ttft_ms:
                            ttft_ms = gw.now_ms() - started
                        filtered = gw.strip_think_blocks(text) or ""
                        if filtered:
                            assembled.append(filtered)
                            yield _sse("response.output_text.delta",
                                       {"type": "response.output_text.delta", "response_id": response_id,
                                        "output_index": 0, "content_index": 0, "delta": filtered})
            except gw.nova_engine.EngineUnavailable as err:
                stages.append(f"engine: {err}")
                yield _sse("response.failed", {"type": "response.failed", "response_id": response_id,
                                               "error": {"message": f"All routing stages failed: {err}",
                                                         "code": 502, "stages": stages}})
                return
            except Exception as err:  # noqa: BLE001
                gw.log.exception("RouterX Free engine responses stream failed")
                stages.append(f"engine: {err.__class__.__name__}: {err}")
                yield _sse("response.failed", {"type": "response.failed", "response_id": response_id,
                                               "error": {"message": f"All routing stages failed ({err.__class__.__name__})",
                                                         "code": 502, "stages": stages}})
                return

        text = "".join(assembled)
        tokens_out = gw.estimate_tokens(text) or 1
        meta = gw.nova_meta(served["upstream_model"], served["provider_name"], served.get("stage", 1),
                            requested, served.get("stage", 1) > 1 or not plan.get("direct", True),
                            gw.routing_summary(plan), handoff=gw.current_handoff(), ttft_ms=ttft_ms)
        final = response_object(requested, text, tokens_in, tokens_out, meta, response_id=response_id,
                                created_at=created_at)
        yield _sse("response.completed", {"type": "response.completed", "sequence_number": 1,
                                          "response": final})
        gw.record_served(db, requested, served, messages, tokens_in, tokens_out)
    except Exception as err:  # noqa: BLE001 - headers already sent
        gw.log.exception("unhandled error in /v1/responses stream")
        try:
            yield _sse("response.failed", {"type": "response.failed", "response_id": response_id,
                                           "error": {"message": f"Gateway stream failed ({err.__class__.__name__})",
                                                     "code": 502}})
        except Exception:
            pass
    finally:
        if committed:
            gw.write_log(db, model=requested, upstream_model=served["upstream_model"],
                         provider_name=served["provider_name"], endpoint=ENDPOINT, status=200,
                         latency_ms=gw.now_ms() - started, tokens_in=tokens_in,
                         tokens_out=gw.estimate_tokens("".join(assembled)) or 1,
                         via=served["via"], spoofed=requested != served["upstream_model"],
                         ttft_ms=ttft_ms)
            try:
                gw.nova_auth.update_usage(db, client_key_id, tokens_in, gw.estimate_tokens("".join(assembled)))
            except Exception:
                pass
        db.close()


__all__ = ["router", "create_response", "input_to_messages", "response_object", "chat_body",
           "_unpack_stream"]
