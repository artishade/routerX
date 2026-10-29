"""Agent API — Python port of src/app/api/agent/**.

Endpoints (prefix /api/agent):
  GET  /tools                    → the agent's tool catalogue (exact TS shape)
  GET  /tasks                    → list tasks (desc by createdAt, steps asc)
  POST /tasks                    {goal, model?, max_steps?} → create AgentTask
                                 (cuid-like id) + start the runner in the
                                 background; responds immediately {id, status}
  GET  /tasks/{task_id}          → single task incl. steps (404 if missing)
  POST /tasks/{task_id}/cancel   → {ok} — marks cancelled; a running loop stops
                                 on its next DB status check
"""
from __future__ import annotations

import math
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from nova.agent import run_agent_task, serialize_agent_task
from nova.database import get_db
from nova.models import AgentTask, utcnow

router = APIRouter()

# GET /api/agent/tools — the agent's tool catalogue (per API contract section 3)
TOOLS = [
    {"id": "web_search", "name": "Web Search", "description": "Search the live web for up-to-date information", "icon": "Globe"},
    {"id": "read_url", "name": "Page Reader", "description": "Read and extract any web page", "icon": "BookOpen"},
    {"id": "terminal", "name": "Terminal", "description": "Run a shell command in the terminal", "icon": "TerminalSquare"},
    {"id": "gateway_stats", "name": "Gateway Telemetry", "description": "Inspect live gateway stats, models and routes", "icon": "Activity"},
    {"id": "storage_scan", "name": "Storage Scanner", "description": "Scan configured storage providers and files", "icon": "HardDrive"},
    {"id": "discover_models", "name": "Model Discovery", "description": "Discover live models available from every configured provider via /v1/models", "icon": "Radar"},
    {"id": "trading_quote", "name": "Market Quote", "description": "Read a live bid/ask/last quote from a configured exchange (exchange::symbol)", "icon": "LineChart"},
    {"id": "trading_positions", "name": "Open Positions", "description": "Read open derivative positions from a configured exchange", "icon": "Coins"},
    {"id": "trading_balances", "name": "Account Balances", "description": "Read account balances from a configured exchange", "icon": "Wallet"},
]


@router.get("/tools")
def list_tools(db: Session = Depends(get_db)):
    """The permanent toolbox + whatever plugins (MCP servers) the user added."""
    payload = [dict(t) for t in TOOLS]
    try:
        from nova import mcp_registry

        for tool in mcp_registry.tool_catalogue(db):
            payload.append({
                "id": f"mcp:{tool['qualified']}",
                "name": tool["qualified"],
                "description": tool["description"] or f"Tool from the {tool['server_name']} MCP server",
                "icon": "Plug",
            })
    except Exception:
        # The plugin registry must never take the builtin catalogue down.
        pass
    return JSONResponse(payload)


@router.get("/tasks")
def list_tasks(db: Session = Depends(get_db)):
    try:
        tasks = db.scalars(
            select(AgentTask)
            .options(joinedload(AgentTask.steps))
            .order_by(AgentTask.createdAt.desc())
        ).unique().all()
        return JSONResponse([serialize_agent_task(t) for t in tasks])
    except Exception as err:
        message = str(err) or "Failed to list agent tasks"
        return JSONResponse({"error": message}, status_code=500)


@router.post("/tasks")
async def create_task(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        body = {}  # TS: req.json().catch(() => ({}))
    if not isinstance(body, dict):
        body = {}

    raw_goal = body.get("goal")
    goal = raw_goal.strip() if isinstance(raw_goal, str) else ""
    if not goal:
        return JSONResponse(
            {"error": "Field 'goal' is required and must be non-empty"}, status_code=400
        )

    raw_max = body.get("max_steps")
    max_steps = None
    if isinstance(raw_max, (int, float)) and not isinstance(raw_max, bool) and math.isfinite(raw_max):
        max_steps = max(1, min(math.floor(raw_max), 24))

    raw_model = body.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else "auto"

    task = AgentTask(id=uuid.uuid4().hex[:25], goal=goal, model=model)
    if max_steps is not None:
        task.maxSteps = max_steps
    db.add(task)
    db.commit()

    # Fire-and-forget: respond immediately, the runner updates the DB as it
    # progresses (FastAPI runs async background tasks after the response).
    background.add_task(run_agent_task, task.id)

    return JSONResponse({"id": task.id, "status": task.status})


@router.get("/tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)):
    try:
        task = db.scalars(
            select(AgentTask)
            .where(AgentTask.id == task_id)
            .options(joinedload(AgentTask.steps))
        ).unique().first()
        if task is None:
            return JSONResponse({"error": "Agent task not found"}, status_code=404)
        return JSONResponse(serialize_agent_task(task))
    except Exception as err:
        message = str(err) or "Failed to load agent task"
        return JSONResponse({"error": message}, status_code=500)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    try:
        task = db.get(AgentTask, task_id)
        if task is None:
            return JSONResponse({"error": "Agent task not found"}, status_code=404)
        task.status = "cancelled"
        if task.finishedAt is None:
            task.finishedAt = utcnow()
        db.commit()
        return JSONResponse({"ok": True})
    except Exception as err:
        message = str(err) or "Failed to cancel agent task"
        return JSONResponse({"error": message}, status_code=500)


# (Mounted by main.py: agent_router.include_router(agent_api.router))
