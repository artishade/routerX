"""Wire-format helpers for upstream providers (not the public gateway format)."""
from __future__ import annotations

import json
import uuid

import httpx


ANTHROPIC_VERSION = "2023-06-01"

# Headers Claude Code attaches for feature pass-through that the gateway must
# forward verbatim to the upstream Anthropic-format provider. Anything not in
# this set is the gateway's to consume or drop.
ANTHROPIC_FORWARD_HEADERS = ("anthropic-beta", "anthropic-version", "anthropic-workspace-id")


def anthropic_url(base: str, path: str = "messages") -> str:
    """Accept both an API origin and a base ending in /v1."""
    root = base.rstrip("/")
    return f"{root}/{path}" if root.endswith("/v1") else f"{root}/v1/{path}"


def anthropic_headers(api_key: str, forward: dict[str, str] | None = None) -> dict[str, str]:
    """Build upstream headers. `api_key` supplies the authentication credential;
    `forward` carries request headers the gateway must pass through unchanged
    (notably `anthropic-beta` so Claude Code's classifier / safeguards billing
    stays server-side and isn't billed to the user's account)."""
    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "anthropic-version": ANTHROPIC_VERSION}
    if forward:
        for _k in ANTHROPIC_FORWARD_HEADERS:
            v = forward.get(_k)
            if v is not None:
                headers[_k] = v
    return headers


def extract_forward_headers(scope: dict | None = None, request=None) -> dict[str, str]:
    """Pull the Anthropic forward headers off an incoming request for echo to
    upstream. Accepts either a FastAPI `Request` or a raw header dict."""
    out: dict[str, str] = {}
    if request is not None:
        for k in ANTHROPIC_FORWARD_HEADERS:
            v = request.headers.get(k)
            if v:
                out[k] = v
    elif isinstance(scope, dict):
        for k, v in scope.items():
            out[k] = v
    return out


def response_json_dict(response: httpx.Response) -> dict | None:
    """Parse an upstream JSON object without ever raising.

    A 2xx is not a promise of JSON: providers answer with HTML challenge pages,
    empty bodies, or arrays when they are degraded. Returning ``None`` lets the
    gateway treat that as "this stage failed" and continue the fallback chain,
    instead of leaking a ``ValueError`` as an HTTP 500.
    """
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def forward_response_headers(response: httpx.Response) -> dict[str, str]:
    """Headers the gateway must pass through on Anthropic-format responses so
    that stall detection, retries, and usage-limit display all work."""
    if not response:
        return {}
    out: dict[str, str] = {}
    for k in ("retry-after", "x-should-retry", "content-type"):
        v = response.headers.get(k)
        if v:
            out[k] = v
    for k in list(response.headers.keys()):
        if k.startswith("anthropic-ratelimit-"):
            out[k] = response.headers[k]
    return out


def _tool_choice(choice):
    if choice == "required":
        return {"type": "any"}
    if choice == "auto":
        return {"type": "auto"}
    if isinstance(choice, dict) and choice.get("type") == "function":
        fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
        name = fn.get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def openai_to_anthropic(model: str, messages: list[dict], options: dict | None = None,
                        stream: bool = False) -> dict:
    """Translate chat messages including tool calls/results to native Messages."""
    options = options or {}
    body: dict = {"model": model, "max_tokens": options.get("max_completion_tokens") or options.get("max_tokens") or 4096,
                  "messages": []}
    if stream:
        body["stream"] = True
    def append_blocks(role: str, blocks: list[dict]) -> None:
        """Append one turn, merging consecutive same-role turns.

        Anthropic wants one user turn carrying every parallel `tool_result`
        block for a tool call. Emitting one user message per tool result makes
        the upstream reject the conversation ("roles must alternate"), which
        used to burn the Anthropic fallback on the second agent turn.
        """
        target = role if role in ("assistant", "user") else "user"
        blocks = blocks or [{"type": "text", "text": ""}]
        if body["messages"] and body["messages"][-1]["role"] == target:
            body["messages"][-1]["content"].extend(blocks)
        else:
            body["messages"].append({"role": target, "content": blocks})

    system: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(str(m.get("content") or ""))
            continue
        if role == "tool":
            tool_content = m.get("content")
            if isinstance(tool_content, list):
                tool_content = "\n".join(
                    str(part.get("text", "")) for part in tool_content
                    if isinstance(part, dict) and "text" in part
                )
            else:
                tool_content = str(tool_content or "")
            append_blocks("user", [{"type": "tool_result",
                                    "tool_use_id": m.get("tool_call_id") or "",
                                    "content": tool_content}])
            continue
        blocks: list[dict] = []
        content = m.get("content")
        if isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, dict) and b.get("type") == "text")
        if role == "assistant":
            for call in m.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                blocks.append({"type": "tool_use", "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                               "name": fn.get("name") or "", "input": args if isinstance(args, dict) else {}})
        append_blocks(role, blocks)
    if system:
        body["system"] = "\n".join(system)
    for field in ("temperature", "top_p", "top_k"):
        if field in options and options[field] is not None:
            body[field] = options[field]
    if options.get("stop"):
        body["stop_sequences"] = options["stop"] if isinstance(options["stop"], list) else [options["stop"]]
    tools = []
    for tool in options.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        if fn.get("name"):
            tools.append({"name": fn["name"], "description": fn.get("description") or "",
                          "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    if tools:
        body["tools"] = tools
        choice = _tool_choice(options.get("tool_choice"))
        if choice:
            body["tool_choice"] = choice
    return body


def native_anthropic_body(payload: dict, model: str, stream: bool = False) -> dict:
    """Preserve native tool_result/tool_use content for /v1/messages clients."""
    body = {"model": model, "max_tokens": payload.get("max_tokens") or 4096, "messages": []}
    if stream:
        body["stream"] = True
    if payload.get("system"):
        body["system"] = payload["system"]
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        copy = {"role": message.get("role"), "content": message.get("content")}
        if isinstance(copy["content"], list):
            copy["content"] = [b for b in copy["content"] if not isinstance(b, dict)
                               or b.get("type") not in ("thinking", "redacted_thinking")]
        body["messages"].append(copy)
    for field in ("temperature", "top_p", "top_k", "stop_sequences", "tools", "tool_choice",
                  "thinking", "safeguards", "metadata"):
        if field in payload and payload[field] is not None:
            body[field] = payload[field]
    return body


def anthropic_to_openai(data: dict) -> dict | None:
    """Normalize a native response for the existing gateway fallback pipeline."""
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        return None
    content = "".join(str(b.get("text") or "") for b in data["content"]
                      if isinstance(b, dict) and b.get("type") == "text")
    tools = [{"id": b.get("id"), "type": "function", "function": {
        "name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in data["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not content and not tools:
        return None
    reason = data.get("stop_reason")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {"content": content or None, "tool_calls": tools or None,
            "upstream_message": {"role": "assistant", "content": content or None, **({"tool_calls": tools} if tools else {})},
            "finish_reason": {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(reason, reason or "stop"),
            "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}}
