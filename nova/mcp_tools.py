"""
MCP tool plumbing: scopes, search, and the skill catalogue.

Three pieces of the RouterX MCP surface, all served from
nova/data/agent_skills.json so they work offline:

  - mcp_scopes / mcp_tool_scopes give every tool the scope it needs, so a
    credential can be narrowed to a subset of them (least privilege)
    instead of "any key can do anything".
  - search_tools() is the lexical scorer that picks which of N registered
    plugin tools to show an agent. Deliberately never compiles a regex out
    of user input, so a hostile query cannot cause backtracking blowups.
  - skills is the curated RouterX agent-skill catalogue the console lists.

nova.mcp_registry stays the source of truth for *which* servers a user
has registered; this module only reasons about the tools they expose.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

DATA_FILE = Path(__file__).resolve().parent / "data" / "agent_skills.json"

# Scoring weights for the lexical tool search.
NAME_PHRASE_BONUS = 25
NAME_TOKEN_BONUS = 6
DESC_PHRASE_BONUS = 20
DESC_TOKEN_BONUS = 3
MIN_LIMIT = 1
MAX_LIMIT = 25

DEFAULT_LIMIT = 8


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    try:
        with DATA_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {"skills": [], "mcp_scopes": [], "mcp_tool_scopes": {}}
    data.setdefault("skills", [])
    data.setdefault("mcp_scopes", [])
    data.setdefault("mcp_tool_scopes", {})
    return data


# --------------------------------------------------------------------------- #
# Scopes: the least-privilege table
# --------------------------------------------------------------------------- #

def scopes() -> list[str]:
    return list(_data()["mcp_scopes"])


def tool_scopes() -> dict[str, list[str]]:
    return {k: list(v) for k, v in _data()["mcp_tool_scopes"].items()}


def required_scopes(tool_name: str) -> list[str]:
    """Scopes a tool needs. Unknown tools need nothing — plugins bring their own."""
    return list(_data()["mcp_tool_scopes"].get(tool_name, []))


def has_scopes(granted: Iterable[str] | None, needed: Sequence[str]) -> bool:
    """Least-privilege check. No scopes granted ⇒ everything is permitted.

    That default matters: RouterX's own console/agent run in-process with no
    scope table, and locking them out would be a regression. A client that
    *does* declare scopes gets them enforced.
    """
    granted_set = {s for s in (granted or ()) if isinstance(s, str) and s}
    if not granted_set:
        return True
    return set(needed or ()).issubset(granted_set)


def filter_by_scope(tools: Sequence[dict[str, Any]],
                    granted: Iterable[str] | None) -> list[dict[str, Any]]:
    granted_list = list(granted or ())
    return [t for t in tools if has_scopes(granted_list, t.get("scopes") or [])]


# --------------------------------------------------------------------------- #
# Tool search: lexical and regex-free
# --------------------------------------------------------------------------- #

def _count_occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    count = 0
    pos = 0
    step = len(needle)
    while (pos := haystack.find(needle, pos)) != -1:
        count += 1
        pos += step
    return count


def _score(entry: dict[str, Any], phrase: str, tokens: Sequence[str]) -> float:
    name = str(entry.get("name", "")).lower()
    desc = str(entry.get("description", "")).lower()
    score = 0.0
    if name and name in phrase:
        score += NAME_PHRASE_BONUS
    for token in tokens:
        score += _count_occurrences(name, token) * NAME_TOKEN_BONUS
    if desc and desc in phrase:
        score += DESC_PHRASE_BONUS
    for token in tokens:
        score += _count_occurrences(desc, token) * DESC_TOKEN_BONUS
    return score


def search_tools(entries: Sequence[dict[str, Any]], query: str,
                 limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Top-K tools by lexical score, ties broken by name.

    Only `str.indexOf` — never `re.compile(query)` — so any query string is safe
    to run against plugin-supplied text.
    """
    clamped = max(MIN_LIMIT, min(MAX_LIMIT, limit))
    phrase = (query or "").strip().lower()
    if not phrase:
        return []
    tokens = [t for t in phrase.split() if t]

    scored: list[dict[str, Any]] = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        score = _score(entry, phrase, tokens)
        if score <= 0:
            continue
        out = dict(entry)
        out["score"] = score
        scored.append(out)

    scored.sort(key=lambda e: (-e["score"], str(e.get("name", ""))))
    return scored[:clamped]


def search_registered(tools: Sequence[dict[str, Any]], query: str,
                      limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Same as `search_tools`, but attaches the scopes each tool requires."""
    results = search_tools(tools, query, limit)
    for r in results:
        r["scopes"] = required_scopes(str(r.get("name", ""))) or list(r.get("scopes") or [])
    return results


# --------------------------------------------------------------------------- #
# Agent skills: the RouterX skill catalogue
# --------------------------------------------------------------------------- #

def skills() -> list[dict[str, Any]]:
    return [dict(s) for s in _data()["skills"]]


def skill(skill_id: str) -> dict[str, Any] | None:
    needle = (skill_id or "").strip().lower()
    for s in _data()["skills"]:
        if s["id"].lower() == needle:
            return dict(s)
    return None


def skill_prompt(skill_id: str) -> str:
    """Compact one-per-line briefing for the agent's system prompt."""
    s = skill(skill_id)
    if not s:
        return ""
    return f"{s['name']} [{s['category']}/{s['area']}]: {s['description']}"


def search_skills(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Substring match over skill name, description and area."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for s in _data()["skills"]:
        hay = f"{s['id']}\n{s['name']}\n{s['description']}\n{s['category']}\n{s['area']}".lower()
        if q in hay:
            out.append(dict(s))
    return out[: max(1, limit)]


__all__ = [
    "DATA_FILE", "scopes", "tool_scopes", "required_scopes", "has_scopes",
    "filter_by_scope", "search_tools", "search_registered",
    "skills", "skill", "skill_prompt", "search_skills",
]
