"""RouterX Agent — autonomous task runner (Python port of src/lib/server/agent.ts).

Loop: plan (LLM, strict JSON) → execute tool → persist AgentStep → repeat until
`finish` or the step limit; then a final LLM call writes the summary. All tool
failures are recorded as error steps and never abort the task; a cancelled
status in the DB stops the loop on the next iteration.

LLM calls go through nova.engine (the free-model sidecar) and fall back to this
server's own /v1/chat/completions when the sidecar is off; every DB touch opens
its own SessionLocal() session — never one session across the whole run. The
runner is an async callable, so routers/agent_api.py can hand it to FastAPI
BackgroundTasks and respond immediately.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import platform
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from . import engine as nova_engine
from .config import PORT
from .database import SessionLocal
from .models import (
    AgentStep,
    AgentTask,
    ClientKey,
    Model,
    Provider,
    RequestLog,
    StorageFile,
    StorageProviderRow,
    utcnow,
)

log = logging.getLogger("nova.agent")

MAX_DETAIL_CHARS = 2000

# Every agent task types its commands into its own terminal tab, so two tasks
# never share a shell and you can read back what each one ran. The label is
# remembered process-wide (the tools are called without a task handle) and is
# set once per run_agent_task().
_ACTIVE_AGENT_LABEL: str | None = None


def agent_label_for(goal: str) -> str:
    from terminal.link import shell_hint_for  # noqa: PLC0415 - avoids an import cycle
    from terminal.pty import agent_label  # noqa: PLC0415 - avoids an import cycle
    return agent_label(goal)


def _terminal_default_cwd() -> str:
    """Agent shell cwd — the builder base location (`/app/build`), kept
    separate from the gateway's own project files."""
    try:
        from .build_registry import build_root
        return str(build_root())
    except Exception:
        return str(__import__("pathlib").Path("/tmp"))

ALLOWED_ACTIONS = {
    "web_search",
    "read_url",
    "terminal",
    "gateway_stats",
    "storage_scan",
    "discover_models",
    "write_file",
    "read_file",
    "edit_file",
    "mkdir",
    "bash_exec",
    "mcp_call",
    "trading_quote",
    "trading_positions",
    "trading_balances",
    "finish",
}

# Human-readable toolbox description — surfaced by routers/build_api.py so
# the web UI can show what is permanently registered for background tasks.
TOOLS_DOC = {
    "web_search": "Search the live web for up-to-date information",
    "read_url": "Read and extract any web page",
    "terminal": "Run diagnostics in the Root@Build cloud shell",
    "gateway_stats": "Inspect live gateway stats, models and routes",
    "storage_scan": "Scan configured cloud storage targets (R2 / GCS / S3 / …)",
    "discover_models": "Discover live models available from every configured provider via /v1/models",
    "write_file": "Create or overwrite a workspace file (<path> ||| <content>)",
    "read_file": "Read a workspace file",
    "edit_file": "Edit a workspace file (<path> ||| <old> ||| <new>)",
    "mkdir": "Create a directory",
    "bash_exec": "Run a real shell command in the cloud workspace",
    "mcp_call": "Call a tool exposed by a registered MCP server (plugin): server::tool",
    "trading_quote": "Read a live bid/ask/last quote from a configured exchange (argument: exchange::symbol or symbol)",
    "trading_positions": "Read open derivative positions from a configured exchange (argument: optional exchange key)",
    "trading_balances": "Read account balances from a configured exchange (argument: optional exchange key)",
    "finish": "End the task with a final summary",
}

AGENT_SYSTEM_PROMPT = """You are RouterX Agent, an autonomous research & operations agent inside the RouterX AI gateway.

You work toward the user's goal one step at a time using these tools:
- web_search: search the live web for up-to-date information ("query_or_url_or_command" = search query)
- read_url: read and extract any web page ("query_or_url_or_command" = full URL, must start with http)
- terminal: run a shell command in the terminal ("query_or_url_or_command" = command, e.g. "free -m", "df -h", "nova status", "nova gpu")
- gateway_stats: inspect live gateway stats, models and routes (no input needed)
- storage_scan: scan configured storage providers and files (no input needed)
- discover_models: discover which AI models are ACTUALLY available right now from the configured providers via /v1/models ("query_or_url_or_command" = optional filter: a provider key like "groq" or "openrouter", or "free" for free-tier models only; leave empty for all providers)
- write_file: CREATE or OVERWRITE a file. query_or_url_or_command = "<path> ||| <full file content>" (split on first " ||| "). Example: "myapp/index.html ||| <!doctype html>..."
- read_file: READ a file. query_or_url_or_command = "<path>" (e.g. "myapp/index.html")
- edit_file: EDIT a file. query_or_url_or_command = "<path> ||| <old string> ||| <new string>" (three-way split)
- mkdir: CREATE a directory. query_or_url_or_command = "<path>"
- bash_exec: RUN a real shell command (bun, npm, pip, python3, build). query_or_url_or_command = "<shell command>" (e.g. "bun install && bun run build")
- mcp_call: CALL a tool from one of the MCP servers (plugins) the user registered — see the "# Available MCP tools" section of the request. query_or_url_or_command = "server_id::tool_name" followed by an optional JSON object of arguments, e.g. 'deepwiki::ask_wiki {"repo":"fastapi/fastapi"}'
- trading_quote: READ-ONLY live bid/ask/last quote from a configured exchange. query_or_url_or_command = "exchange::symbol" (e.g. "binance::BTC/USDT") or just "symbol" when only one exchange is available
- trading_positions: READ-ONLY open derivative positions from a configured exchange. query_or_url_or_command = optional exchange key (e.g. "binance"); leave empty to use the only configured exchange
- trading_balances: READ-ONLY account balances from a configured exchange. query_or_url_or_command = optional exchange key (e.g. "binance"); leave empty to use the only configured exchange
- finish: the goal is achieved ("query_or_url_or_command" = final key takeaway, optional)

STRICT OUTPUT RULE — respond with JSON only, no prose, no markdown fences, exactly this shape:
{"action": "web_search|read_url|terminal|gateway_stats|storage_scan|discover_models|mcp_call|trading_quote|trading_positions|trading_balances|finish", "title": "short title", "query_or_url_or_command": "...", "reason": "why this step"}

Rules:
1. Exactly one action per response.
2. Titles are short labels (max 60 chars); "reason" explains why this step helps the goal.
3. Prefer web_search then read_url to gather evidence; use terminal/gateway_stats/storage_scan for system questions; use discover_models whenever the goal involves finding, comparing or choosing AI models or providers.
4. Never repeat a step that already succeeded with the same input — read the step log first.
5. As soon as the goal is achieved (or no further step adds value), respond with action "finish".
6. NEVER output fake XML like <tool_call>, <invoke>, <write>, ```bash hallucinations. ONLY output the single JSON object described above. Real file writes happen via write_file - dont hallucinate them as text."""

FINAL_SYSTEM_PROMPT = (
    "Write the final answer to the goal in concise markdown (≤200 words), using the evidence gathered."
)


# --------------------------------------------------------------------------- #
# Serialization (snake_case wire format per src/lib/types.ts)
# --------------------------------------------------------------------------- #

def _epoch_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def serialize_agent_task(task: AgentTask) -> dict:
    return {
        "id": task.id,
        "goal": task.goal,
        "status": task.status,
        "model": task.model,
        "max_steps": task.maxSteps,
        "summary": task.summary,
        "error": task.error,
        "created_at": _epoch_ms(task.createdAt),
        "started_at": _epoch_ms(task.startedAt),
        "finished_at": _epoch_ms(task.finishedAt),
        "steps": [
            {
                "id": s.id,
                "step_number": s.stepNumber,
                "action": s.action,
                "title": s.title,
                "description": s.description,
                "detail": s.detail,
                "status": s.status,
                "latency_ms": s.latencyMs,
                "created_at": _epoch_ms(s.createdAt),
            }
            for s in task.steps
        ],
    }


# --------------------------------------------------------------------------- #
# Planning (LLM)
# --------------------------------------------------------------------------- #

@dataclass
class AgentPlan:
    action: str
    title: str
    query: str
    reason: str


def _recover_hallucinated_write(raw: str):
    import re as _re2
    tc = "<tool_call>"
    iv = "<invoke"
    if tc not in raw and iv not in raw.lower():
        return None
    fp = _re2.search(r"<parameter[^>]*name\s*=\s*[\x22\x27]?file_path[\x22\x27]?[^>]*>(.*?)</parameter>", raw, _re2.DOTALL | _re2.IGNORECASE)
    ct = _re2.search(r"<parameter[^>]*name\s*=\s*[\x22\x27]?content[\x22\x27]?[^>]*>(.*?)</parameter>", raw, _re2.DOTALL | _re2.IGNORECASE)
    if not fp or not ct:
        return None
    from dataclasses import dataclass
    return AgentPlan(action="write_file", title="Recovered write", query=fp.group(1).strip() + " ||| " + ct.group(1), reason="Recovered from leaked tool-call XML")

def parse_plan(raw: str) -> AgentPlan | None:
    """Defensively parse the planner output: strip code fences, find the first
    {...} block (verbatim port of parsePlan)."""
    if not raw:
        return None
    recovered = _recover_hallucinated_write(raw)
    if recovered is not None:
        return recovered
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.IGNORECASE)
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    def as_str(value) -> str:
        return "" if value is None else str(value)

    action = as_str(obj.get("action")).strip()
    if action not in ALLOWED_ACTIONS:
        return None
    return AgentPlan(
        action=action,
        title=as_str(obj.get("title")).strip()[:120] or action,
        query=as_str(obj.get("query_or_url_or_command")).strip(),
        reason=as_str(obj.get("reason")).strip(),
    )


def build_planner_user_prompt(goal: str, steps: list[AgentStep]) -> str:
    if not steps:
        step_log = "(no steps yet — this is the beginning of the task)"
    else:
        lines = []
        for s in steps:
            detail = _squash(s.detail)[:200] or "(no detail)"
            flag = " [FAILED]" if s.status == "error" else ""
            lines.append(f"{s.stepNumber}. [{s.action}{flag}] {s.title} → {detail}")
        step_log = "\n".join(lines)
    return (
        f"# Goal\n{goal}\n\n# Step log so far\n{step_log}\n\n"
        f"{_mcp_catalogue_prompt()}\n"
        'Decide the next single action. Respond with STRICT JSON per the system rules. '
        'Use action "finish" when the goal is achieved.'
    )


def _mcp_catalogue_prompt() -> str:
    """The user's plugins, as a plain list the planner can pick from.

    Read from the cached snapshot (never probed here), so a slow or dead
    plugin can never stall a task — it simply is not offered.
    """
    try:
        from . import mcp_registry  # noqa: PLC0415 - avoids an import cycle

        with SessionLocal() as db:
            tools = mcp_registry.tool_catalogue(db)
    except Exception as err:  # a broken registry must not break the planner
        log.warning("MCP catalogue unavailable: %s", err)
        return "# Available MCP tools\n(none — the plugin registry could not be read)"
    if not tools:
        return "# Available MCP tools\n(none registered — use the builtin tools)"
    lines = [
        f"- {t['qualified']} — {t['description'] or 'no description'}"
        for t in tools[:60]
    ]
    return "# Available MCP tools\n" + "\n".join(lines)


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _completion_content(completion) -> str:
    try:
        content = completion["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else str(content or "")


# --------------------------------------------------------------------------- #
# The agent's own brain
# --------------------------------------------------------------------------- #
# The planner and the final summary are LLM calls, and they used to have exactly
# one source: the builtin RouterX Free engine sidecar. That sidecar can legitimately
# be off (NOVA_ENGINE_DISABLED, no bun/node runtime, no reachable free
# catalogue) — and then every task died with "Agent planner returned unparseable
# output twice" before reaching a single tool, while the chat box right next to
# it answered happily through the configured providers. So the sidecar stays
# first (it is free and keyless) and the gateway is the fallback: the very same
# OpenAI-shaped call the console chat makes, looped back over 127.0.0.1.

AGENT_LLM_TIMEOUT_S = 90.0


def _default_model_id() -> str | None:
    """A concrete exposed model id to plan with.

    The gateway routes by model id (plus that route's fallback chain), so
    `auto` is not something it can answer on its own — a task left on `auto`
    used to come back 502 "All routing stages failed" even with working
    providers. Prefer a healthy, enabled, cheap model; free first so a task
    never quietly spends the user's credits when a free tier exists.
    """
    try:
        with SessionLocal() as db:
            rows = db.scalars(
                select(Model)
                .where(Model.enabled.is_(True))
                .options(joinedload(Model.provider))
            ).unique().all()
    except Exception:
        return None
    usable = [m for m in rows if getattr(m.provider, "enabled", False)]
    if not usable:
        return None
    healthy = [m for m in usable if m.status == "healthy"] or usable
    healthy.sort(key=lambda m: (not bool(m.isFree), m.latencyMs or 10**6, m.displayName or m.exposedId))
    return healthy[0].exposedId or None


async def _gateway_chat(messages: list[dict], model: str = "auto",
                        timeout: float = AGENT_LLM_TIMEOUT_S) -> dict:
    """One completion through this server's own /v1/chat/completions."""
    url = f"http://127.0.0.1:{PORT}/v1/chat/completions"
    wanted = (model or "").strip()
    candidates = [wanted] if wanted and wanted != "auto" else []
    default = _default_model_id()
    if default and default not in candidates:
        candidates.append(default)
    if not candidates:
        raise RuntimeError("no enabled model to plan with — add a provider or enable a model")
    failure = ""
    for candidate in candidates:
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                res = await client.post(url, json={"model": candidate, "messages": messages})
        except httpx.HTTPError as err:
            failure = f"gateway unreachable: {err}"
            continue
        if res.status_code < 400:
            return res.json()
        detail = ""
        try:
            detail = str(res.json().get("error") or "")
        except Exception:
            detail = res.text[:200]
        failure = f"gateway HTTP {res.status_code} on {candidate}: {detail or 'no detail'}"
    raise RuntimeError(failure)


async def agent_llm(messages: list[dict], model: str = "auto",
                    timeout: float = AGENT_LLM_TIMEOUT_S) -> dict:
    """Engine sidecar first, gateway second. Never returns a non-dict."""
    try:
        return await nova_engine.chat(messages, timeout=timeout)
    except Exception as err:
        log.info("agent: engine sidecar unavailable (%s) — using the gateway", err)
    return await _gateway_chat(messages, model, timeout)


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def strip_html(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def tool_web_search(query: str) -> str:
    q = query or "RouterX AI gateway"
    results = await nova_engine.web_search(q)
    if not isinstance(results, list) or not results:
        return f'No results for "{q}".'
    lines = []
    for i, r in enumerate(results):
        r = r if isinstance(r, dict) else {}
        name = r.get("name")
        url = r.get("url")
        snippet = r.get("snippet")
        lines.append(
            f"{i + 1}. {name}\n   {url}\n   {('' if snippet is None else str(snippet)).strip()[:200]}"
        )
    return "\n".join(lines)


async def tool_read_url(url: str) -> str:
    page = await nova_engine.read_url(url)
    data = page if isinstance(page, dict) else {}
    inner = data["data"] if isinstance(data.get("data"), dict) else data
    title = inner.get("title") or url
    text = strip_html(str(inner.get("html") or inner.get("text") or ""))[:1500]
    return f"{title}\n\n{text or '(no extractable text)'}"


def fallback_system_stats(command: str) -> str:
    """OS-level diagnostics when the safe executor module (terminal.sandbox,
    written by agent 2-c) is not importable yet — port of fallbackSystemStats."""
    try:
        total_mb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except Exception:
        total_mb = 0
    free_mb = _mem_available_mb()
    cpus = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except Exception:
        load = 0.0
    load_pct = min(99, round(load / max(1, cpus) * 100))
    mem_pct = round(free_mb / max(1, total_mb) * 100)
    uptime_s = _uptime_seconds()
    return "\n".join([
        f"$ {command}",
        "[sandbox fallback] Safe executor module unavailable — basic system diagnostics only:",
        f"Host: {socket.gethostname()} · {platform.system()} {platform.machine()} · Python {sys.version.split()[0]}",
        f"CPUs: {cpus} · Load: {load_pct}%",
        f"Memory: {free_mb} MB free / {total_mb} MB total ({mem_pct}% free)",
        f"Uptime: {uptime_s}s",
    ])


def _mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES") / (1024 * 1024))
    except Exception:
        return 0


def _uptime_seconds() -> int:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except Exception:
        return 0


def _exec_field(res, key: str):
    if isinstance(res, dict):
        return res.get(key)
    return getattr(res, key, None)


async def run_in_live_terminal(command: str, timeout: int = 45,
                             label: str | None = None) -> str | None:
    """Run `command` in the terminal tab the user is watching, or None.

    The agent's shell steps go through a real PTY session (labelled `agent`,
    listed in the session tabs) so a task in the chatbox visibly runs its
    commands in the terminal instead of vanishing into a subprocess. Returns
    None when no live session can be had, and the caller then falls back to
    its original one-shot executor — the agent never loses the ability to run
    a command just because nobody is watching.

    Goes through `terminal.link`, so this works unchanged when the terminal is
    hosted as its own service: the step still lands in a visible tab there.

    Off-thread on purpose: run_agent_task is an async callable handed to
    FastAPI BackgroundTasks, so it executes on the event loop.
    """
    from terminal.link import TerminalError, current  # noqa: PLC0415

    cmd = (command or "").strip()
    if not cmd:
        return None
    try:
        result = await current().run_command(
            cmd, label if label is not None else _ACTIVE_AGENT_LABEL, timeout)
    except TerminalError as exc:
        logging.getLogger("nova.agent").warning("live terminal step failed: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logging.getLogger("nova.agent").warning("live terminal step failed: %s", exc)
        return None
    if result is None:
        return None
    out, code = result
    if code == -1:
        return f"$ {cmd}\n{out or '(no output)'}\n[still running or timed out — check the agent terminal tab]"
    tail = "" if code == 0 else f"\n[exit {code}]"
    return f"$ {cmd}\n{out or '(no output)'}{tail}"


async def tool_terminal(command: str) -> str:
    """Run a command through agent 2-c's safe executor. terminal.sandbox is
    imported LAZILY inside this function (it may not exist yet while 2-c works
    concurrently); if it is unavailable we degrade to honest OS stats, exactly
    like the TypeScript fallback."""
    cmd = command or "nova status"
    live = await run_in_live_terminal(cmd)
    if live is not None:
        return live
    try:
        from terminal.sandbox import execute_command  # noqa: PLC0415 — lazy by design
    except Exception:
        return fallback_system_stats(cmd)

    db: Session = SessionLocal()
    try:
        try:
            res = execute_command(db, cmd, _terminal_default_cwd())
        except TypeError:
            # signature drift guard — try the 2-arg variant before giving up
            res = execute_command(db, cmd)
        if inspect.isawaitable(res):
            res = await res
    except Exception:
        return fallback_system_stats(cmd)
    finally:
        db.close()

    out = _exec_field(res, "output")
    if out is None:
        parts = [
            str(x)
            for x in (_exec_field(res, "stdout"), _exec_field(res, "stderr"))
            if x and str(x).strip()
        ]
        out = "\n".join(parts)
    out = str(out).strip()
    return f"$ {cmd}\n{out or '(no output)'}"


async def tool_gateway_stats() -> str:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    with SessionLocal() as db:
        providers = db.scalar(select(func.count()).select_from(Provider)) or 0
        enabled_providers = (
            db.scalar(select(func.count()).select_from(Provider).where(Provider.enabled.is_(True))) or 0
        )
        models = db.scalar(select(func.count()).select_from(Model)) or 0
        healthy = (
            db.scalar(
                select(func.count())
                .select_from(Model)
                .where(Model.status == "healthy", Model.enabled.is_(True))
            )
            or 0
        )
        client_keys = db.scalar(select(func.count()).select_from(ClientKey)) or 0
        logs = db.execute(
            select(RequestLog.status, RequestLog.latencyMs).where(RequestLog.ts >= since)
        ).all()

    errors = sum(1 for status, _ in logs if status >= 400)
    avg_latency = _js_round(sum(lat for _, lat in logs) / len(logs)) if logs else 0
    err_pct = f"{(errors / len(logs)) * 100:.1f}" if logs else "0.0"
    return "\n".join([
        "Gateway telemetry (live):",
        f"• Providers: {providers} registered · {enabled_providers} enabled",
        f"• Models: {models} in catalogue · {healthy} healthy",
        f"• Client keys: {client_keys}",
        f"• Requests (24h): {len(logs)} · Errors: {errors} ({err_pct}%) · Avg latency: {avg_latency} ms",
    ])


async def tool_storage_scan() -> str:
    with SessionLocal() as db:
        providers = db.scalars(
            select(StorageProviderRow).order_by(
                StorageProviderRow.active.desc(), StorageProviderRow.id.asc()
            )
        ).all()
        files = db.execute(select(StorageFile.name, StorageFile.size)).all()

    lines = []
    for p in providers:
        flag = "ACTIVE" if p.active else p.status
        usage = (
            f"— {(p.usageMb or 0):.1f}/{(p.quotaMb or 0):.0f} MB"
            if (p.quotaMb or 0) > 0
            else f"— {(p.usageMb or 0):.1f} MB used"
        )
        tier = f" · free tier: {p.freeTier}" if p.freeTier else ""
        lines.append(f"• {p.id} ({p.type}): {flag} {usage}{tier}")

    total_bytes = sum(size for _, size in files)
    return "\n".join([
        "Storage scan:",
        *(lines if lines else ["• (no storage providers configured)"]),
        f"Files stored: {len(files)} (total {total_bytes / (1024 * 1024):.2f} MB)",
    ])



LEAKED_TOOL_RE = __import__('re').compile(r"<tool_call>.*?</tool_call>|<invoke.*?</invoke>", __import__('re').DOTALL | __import__('re').IGNORECASE)
def _strip_leaked(text: str) -> str:
    import re as _re
    if not text:
        return text
    return _re.sub(r"<tool_call>.*?</tool_call>|<invoke.*?</invoke>|```\s*(?:bash|shell).*?```", "", text, flags=_re.DOTALL|_re.IGNORECASE).strip()

async def tool_write_file(arg: str) -> str:
    if "|||" not in arg:
        return 'write_file error: need "<path> ||| <content>" got: ' + arg[:120]
    path, content = arg.split("|||", 1)
    path=path.strip(); content=content.lstrip("\n")
    if not path: return "write_file error: empty path"
    try:
        from terminal.sandbox import builder_write
        res = builder_write(path, content)
        return (res.get("output") or str(res))[:2000]
    except Exception as e: return f"write_file failed: {e}"
async def tool_read_file(arg: str) -> str:
    path=(arg or "").strip()
    if not path: return "read_file error: empty path"
    try:
        from terminal.sandbox import builder_read
        res = builder_read(path)
        return (res.get("output") or str(res))[:2000]
    except Exception as e: return f"read_file failed: {e}"
async def tool_edit_file(arg: str) -> str:
    parts=arg.split("|||", 2)
    if len(parts)!=3: return 'edit_file error: need <path> ||| <old> ||| <new> got: '+arg[:200]
    path,old,new=parts[0].strip(),parts[1],parts[2]
    try:
        from terminal.sandbox import builder_edit
        res = builder_edit(path, old, new)
        return (res.get("output") or str(res))[:2000]
    except Exception as e: return f"edit_file failed: {e}"
async def tool_mkdir(arg: str) -> str:
    path=(arg or "").strip()
    if not path: return "mkdir error: empty"
    try:
        from terminal.sandbox import builder_mkdir
        res = builder_mkdir(path)
        return (res.get("output") or str(res))[:2000]
    except Exception as e: return f"mkdir failed: {e}"
async def tool_bash_exec(arg: str) -> str:
    cmd=(arg or "").strip()
    if not cmd: return "bash_exec error: empty"
    live = await run_in_live_terminal(cmd, timeout=120)
    if live is not None:
        return live[:8000]
    try:
        from terminal.sandbox import builder_bash
        res = builder_bash(cmd)
        return (res.get("output") or str(res))[:2000]
    except Exception as e: return f"bash_exec failed: {e}"

async def tool_mcp_call(argument: str) -> str:
    """Call a tool on one of the user's registered MCP servers (plugins).

    `argument` is `server_id::tool_name` plus an optional JSON object, or a
    single JSON object. Tools are offered from the cached catalogue, so a call
    only fails when the plugin itself is unreachable — and then it says so.
    """
    from .mcp_client import McpError, parse_call  # noqa: PLC0415 - lazy by design
    from . import mcp_registry  # noqa: PLC0415

    raw = (argument or "").strip()
    if not raw:
        return "mcp_call error: use `server::tool` — see the available MCP tools"
    try:
        server_id, tool, args = parse_call(raw)
    except McpError as err:
        return f"mcp_call error: {err}"
    try:
        with SessionLocal() as db:
            output = await mcp_registry.invoke(db, server_id, tool, args)
    except McpError as err:
        return f"mcp_call {server_id}::{tool} failed: {err}"
    except Exception as err:  # pragma: no cover - defensive
        return f"mcp_call {server_id}::{tool} failed: {err.__class__.__name__}: {err}"
    return f"mcp {server_id}::{tool}\n{output}"


async def tool_discover_models(query: str) -> str:
    """Live model discovery via nova.discovery (port of toolDiscoverModels)."""
    q = (query or "").strip().lower()
    is_free_filter = q in ("free", "free models")
    provider_filter = None if is_free_filter or not q else q
    try:
        from .discovery import discover_provider_models, summarize_for_agent  # noqa: PLC0415

        with SessionLocal() as db:
            results = await discover_provider_models(db, provider_filter)
        if is_free_filter:
            filtered = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                free_models = [m for m in r.get("models", []) if m.get("is_free")]
                filtered.append({**r, "models": free_models, "count": len(free_models)})
            results = filtered
        return summarize_for_agent(results, "free" if is_free_filter else provider_filter)
    except Exception as err:
        return f"Model discovery failed: {err}"


def _trading_split(arg: str) -> tuple[str | None, str]:
    text = (arg or "").strip()
    if "::" in text:
        exchange, rest = text.split("::", 1)
        return exchange.strip() or None, rest.strip()
    return None, text


def _fmt_amount(value) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


async def tool_trading_quote(argument: str) -> str:
    try:
        from . import trading  # noqa: PLC0415 - lazy by design
    except Exception as err:
        return f"trading_quote failed: {err.__class__.__name__}: {err}"

    exchange, symbol = _trading_split(argument)
    if not symbol:
        return "trading_quote error: use 'exchange::symbol' (e.g. binance::BTC/USDT)"
    try:
        with SessionLocal() as db:
            quote = await trading.read_quote(
                db, symbol=symbol, exchange=exchange, actor="agent"
            )
    except trading.TradingError as err:
        return f"trading_quote refused: {err.code} — {err.message}"
    except Exception as err:
        return f"trading_quote failed: {err.__class__.__name__}: {err}"
    data = quote if isinstance(quote, dict) else {}
    symbol_out = data.get("symbol") or symbol
    exchange_out = data.get("exchange") or exchange or "auto"
    return "\n".join([
        f"{symbol_out} on {exchange_out}",
        f"bid: {_fmt_amount(data.get('bid'))} · ask: {_fmt_amount(data.get('ask'))} · last: {_fmt_amount(data.get('last'))}",
        f"as of {data.get('ts') or 'unknown'}",
    ])


async def tool_trading_positions(argument: str) -> str:
    try:
        from . import trading  # noqa: PLC0415 - lazy by design
    except Exception as err:
        return f"trading_positions failed: {err.__class__.__name__}: {err}"

    exchange, rest = _trading_split(argument)
    if exchange is None and rest:
        exchange = rest
    try:
        with SessionLocal() as db:
            result = await trading.read_positions(db, exchange=exchange, actor="agent")
    except trading.TradingError as err:
        return f"trading_positions refused: {err.code} — {err.message}"
    except Exception as err:
        return f"trading_positions failed: {err.__class__.__name__}: {err}"
    data = result if isinstance(result, dict) else {}
    exchange_out = data.get("exchange") or exchange or "auto"
    lines = [f"Open positions on {exchange_out} (as of {data.get('as_of') or 'unknown'}):"]
    rows = data.get("positions") if isinstance(data.get("positions"), list) else []
    if not rows:
        lines.append("(no open positions)")
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"· {row.get('symbol')} {row.get('side')} · quantity {_fmt_amount(row.get('quantity'))}"
            f" · entry_price {_fmt_amount(row.get('entry_price'))}"
            f" · mark_price {_fmt_amount(row.get('mark_price'))}"
            f" · unrealized_pnl {_fmt_amount(row.get('unrealized_pnl'))}"
            f" · leverage {_fmt_amount(row.get('leverage'))}"
        )
    return "\n".join(lines)


async def tool_trading_balances(argument: str) -> str:
    try:
        from . import trading  # noqa: PLC0415 - lazy by design
    except Exception as err:
        return f"trading_balances failed: {err.__class__.__name__}: {err}"

    exchange, rest = _trading_split(argument)
    if exchange is None and rest:
        exchange = rest
    try:
        with SessionLocal() as db:
            result = await trading.read_balances(db, exchange=exchange, actor="agent")
    except trading.TradingError as err:
        return f"trading_balances refused: {err.code} — {err.message}"
    except Exception as err:
        return f"trading_balances failed: {err.__class__.__name__}: {err}"
    data = result if isinstance(result, dict) else {}
    exchange_out = data.get("exchange") or exchange or "auto"
    lines = [f"Balances on {exchange_out} (as of {data.get('as_of') or 'unknown'}):"]
    rows = data.get("balances") if isinstance(data.get("balances"), list) else []
    if not rows:
        lines.append("(no non-zero balances)")
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"· {row.get('asset')} free {_fmt_amount(row.get('free'))}"
            f" · locked {_fmt_amount(row.get('locked'))}"
            f" · total {_fmt_amount(row.get('total'))}"
        )
    return "\n".join(lines)


async def execute_tool(plan: AgentPlan) -> str:
    if plan.action == "web_search":
        return await tool_web_search(plan.query)
    if plan.action == "read_url":
        return await tool_read_url(plan.query)
    if plan.action == "terminal":
        return await tool_terminal(plan.query)
    if plan.action == "gateway_stats":
        return await tool_gateway_stats()
    if plan.action == "storage_scan":
        return await tool_storage_scan()
    if plan.action == "discover_models":
        return await tool_discover_models(plan.query)
    if plan.action == "write_file":
        return await tool_write_file(plan.query)
    if plan.action == "read_file":
        return await tool_read_file(plan.query)
    if plan.action == "edit_file":
        return await tool_edit_file(plan.query)
    if plan.action == "mkdir":
        return await tool_mkdir(plan.query)
    if plan.action == "bash_exec":
        return await tool_bash_exec(plan.query)
    if plan.action == "mcp_call":
        return await tool_mcp_call(plan.query)
    if plan.action == "trading_quote":
        return await tool_trading_quote(plan.query)
    if plan.action == "trading_positions":
        return await tool_trading_positions(plan.query)
    if plan.action == "trading_balances":
        return await tool_trading_balances(plan.query)
    raise ValueError(f"Unknown action: {plan.action}")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _js_round(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


async def run_agent_task(task_id: str) -> None:
    """Fire-and-forget runner — background task; every failure lands in the
    AgentTask row, never raised out of the loop."""
    try:
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            if task is None or task.status in ("cancelled", "completed", "failed"):
                return
            task.status = "running"
            if task.startedAt is None:
                task.startedAt = utcnow()
            task.error = None
            goal = task.goal
            task_model = task.model or "auto"
            max_steps = max(1, min(task.maxSteps or 8, 24))
            db.commit()

        # This task gets its own terminal tab (one shell per task, so steps
        # keep their state and nothing lands in the session you are typing in).
        global _ACTIVE_AGENT_LABEL
        _ACTIVE_AGENT_LABEL = agent_label_for(goal)

        parse_retried = False  # one retry after an unparseable planner response

        for _ in range(max_steps):
            # Honor cancellation between steps.
            with SessionLocal() as db:
                current = db.get(AgentTask, task_id)
                if current is None or current.status == "cancelled":
                    return
                steps = list(
                    db.scalars(
                        select(AgentStep)
                        .where(AgentStep.taskId == task_id)
                        .order_by(AgentStep.stepNumber)
                    ).all()
                )
            next_step_number = (steps[-1].stepNumber if steps else 0) + 1

            # ── Plan via LLM ──
            plan_start = time.monotonic()
            raw = ""
            plan: AgentPlan | None = None
            try:
                completion = await agent_llm([
                    {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                    {"role": "user", "content": build_planner_user_prompt(goal, steps)},
                ], task_model)
                raw = _completion_content(completion)
                plan = parse_plan(raw)
            except Exception as err:
                raw = f"LLM call failed: {err}"
            plan_latency = _elapsed_ms(plan_start)

            if plan is None:
                # Unparseable / failed planner output → record a 'think' step,
                # retry once, then fail.
                if parse_retried:
                    with SessionLocal() as db:
                        t = db.get(AgentTask, task_id)
                        if t is not None:
                            t.status = "failed"
                            t.error = "Agent planner returned unparseable output twice"
                            t.finishedAt = utcnow()
                            db.commit()
                    return
                parse_retried = True
                with SessionLocal() as db:
                    db.add(AgentStep(
                        taskId=task_id,
                        stepNumber=next_step_number,
                        action="think",
                        title="Planning (unparsed response)",
                        description="Planner output was not valid JSON — retrying once.",
                        detail=raw[:MAX_DETAIL_CHARS],
                        status="error",
                        latencyMs=plan_latency,
                    ))
                    db.commit()
                continue
            parse_retried = False

            # ── Finish action ──
            if plan.action == "finish":
                with SessionLocal() as db:
                    db.add(AgentStep(
                        taskId=task_id,
                        stepNumber=next_step_number,
                        action="finish",
                        title=plan.title,
                        description=plan.reason,
                        detail=plan.query or plan.reason or "Goal achieved.",
                        status="info",
                        latencyMs=plan_latency,
                    ))
                    db.commit()
                break

            # ── Execute tool with timing ──
            tool_start = time.monotonic()
            status = "ok"
            try:
                detail = await execute_tool(plan)
            except Exception as err:
                detail = f"Tool error: {err}"
                status = "error"
            tool_latency = _elapsed_ms(tool_start)

            with SessionLocal() as db:
                db.add(AgentStep(
                    taskId=task_id,
                    stepNumber=next_step_number,
                    action=plan.action,
                    title=plan.title,
                    description=plan.reason,
                    detail=detail[:MAX_DETAIL_CHARS],
                    status=status,
                    latencyMs=tool_latency,
                ))
                db.commit()

        # Honor cancellation after the loop as well.
        with SessionLocal() as db:
            after_loop = db.get(AgentTask, task_id)
            if after_loop is None or after_loop.status == "cancelled":
                return

        # ── Final summary via LLM ──
        with SessionLocal() as db:
            all_steps = list(
                db.scalars(
                    select(AgentStep)
                    .where(AgentStep.taskId == task_id)
                    .order_by(AgentStep.stepNumber)
                ).all()
            )
        evidence = (
            "\n\n".join(f"{s.stepNumber}. [{s.action}] {s.title}\n{s.detail}" for s in all_steps)
            or "(no steps recorded)"
        )
        summary = ""
        try:
            completion = await agent_llm([
                {"role": "system", "content": FINAL_SYSTEM_PROMPT},
                {"role": "user", "content": f"# Goal\n{goal}\n\n# Evidence gathered\n{evidence}"},
            ], task_model)
            summary = _completion_content(completion).strip()
        except Exception:
            summary = ""

        if not summary:
            # Graceful fallback if the final LLM call fails.
            parts = []
            for s in all_steps:
                line = f"- **{s.title}** ({s.action})"
                if s.detail:
                    line += f": {_squash(s.detail)[:160]}"
                parts.append(line)
            summary = "\n".join(parts) or "Task ended without recorded steps."

        with SessionLocal() as db:
            t = db.get(AgentTask, task_id)
            if t is not None:
                t.status = "completed"
                t.summary = summary
                t.finishedAt = utcnow()
                db.commit()
    except Exception as err:
        try:
            with SessionLocal() as db:
                t = db.get(AgentTask, task_id)
                if t is not None:
                    t.status = "failed"
                    t.error = str(err)
                    t.finishedAt = utcnow()
                    db.commit()
        except Exception:
            pass
