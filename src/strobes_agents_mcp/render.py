"""Compact, model-friendly views of Strobes payloads.

Agent messages carry rich ``blocks`` (text, thinking, tool calls, UI nodes …);
a single message can be ~100 KB, mostly reasoning. By default we keep what a
caller needs — the agent's prose and a one-line note per tool call — and drop
the rest.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

TERMINAL_TASK = {"completed", "failed", "cancelled", "skipped"}


def loads(v: Any) -> Any:
    """Graphene JSON/GenericScalar fields sometimes arrive as JSON strings."""
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def compact(obj: Any) -> Any:
    """Drop None / empty values recursively (saves tokens)."""
    if isinstance(obj, dict):
        return {k: compact(v) for k, v in obj.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    return obj


def lower(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… [+{len(text) - limit} chars]"


def _tool_line(b: dict[str, Any]) -> str | None:
    name = b.get("name") or b.get("tool") or b.get("tool_name")
    if not name:
        return None
    args = b.get("input") or b.get("args") or b.get("arguments") or {}
    args = loads(args)
    hint = ""
    if isinstance(args, dict):
        for k in ("command", "url", "query", "path", "objective", "title", "code"):
            if args.get(k):
                hint = f" {k}={truncate(str(args[k]).splitlines()[0], 100)}"
                break
    return f"[tool {name}{hint}]"


def blocks_to_text(blocks: Any, *, include_thinking: bool = False,
                   include_tools: bool = True) -> str:
    blocks = loads(blocks)
    if not isinstance(blocks, list):
        return str(blocks or "")
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            parts.append(b["text"].strip())
        elif t == "thinking" and include_thinking and b.get("thinking"):
            parts.append(f"<thinking>{b['thinking'].strip()}</thinking>")
        elif include_tools and t in ("tool_use", "tool_call", "tool"):
            line = _tool_line(b)
            if line:
                parts.append(line)
        elif t in ("error",) and (b.get("message") or b.get("text")):
            parts.append(f"[error] {b.get('message') or b.get('text')}")
    return "\n\n".join(p for p in parts if p)


def render_message(m: dict[str, Any], *, include_thinking: bool = False,
                   max_chars: int = 4000) -> dict[str, Any]:
    text = blocks_to_text(m.get("blocks"), include_thinking=include_thinking)
    return compact({
        "id": m.get("id"),
        "author": lower(m.get("author")),
        "agent": m.get("agentName") or m.get("agentId"),
        "status": lower(m.get("status")),
        "created_at": m.get("createdAt"),
        "text": truncate(text, max_chars),
    })


def final_answer(messages: Iterable[dict[str, Any]], max_chars: int = 6000) -> str | None:
    """Prose of the most recent non-user message (messages newest-first or any order)."""
    msgs = sorted((m for m in messages if lower(m.get("author")) != "user"),
                  key=lambda m: m.get("createdAt") or "", reverse=True)
    for m in msgs:
        text = blocks_to_text(m.get("blocks"), include_tools=False)
        if text:
            return truncate(text, max_chars)
    return None


def task(t: dict[str, Any], *, with_instructions: bool = False) -> dict[str, Any]:
    out = {
        "id": t.get("id"),
        "title": t.get("title"),
        "agent": t.get("agentType"),
        "type": lower(t.get("taskType")),
        "status": lower(t.get("status")),
        "error": t.get("errorMessage"),
        "created_at": t.get("createdAt"),
        "started_at": t.get("startedAt"),
        "completed_at": t.get("completedAt"),
        "duration_seconds": t.get("durationSeconds"),
        "created_by": lower(t.get("createdByType")),
    }
    if with_instructions:
        out["instructions"] = t.get("instructions")
    od = loads(t.get("outputData"))
    if isinstance(od, dict):
        out["thread_id"] = od.get("thread_id")
        out["findings_count"] = od.get("findings_count")
    return compact(out)


def workspace(w: dict[str, Any] | None) -> dict[str, Any]:
    if not w:
        return {}
    out = {
        "id": w.get("id"),
        "name": w.get("name"),
        "status": lower(w.get("status")),
        "type": lower(w.get("workspaceType")),
        "description": w.get("description"),
        "template": w.get("templateSlug"),
        "supervisor_mode": lower(w.get("supervisorMode")),
        "engagement_id": w.get("engagementId"),
        "engagement": w.get("engagementName"),
        "created_by": w.get("createdByEmail") or w.get("createdByName"),
        "created_at": w.get("createdAt"),
        "updated_at": w.get("updatedAt"),
        "custom_instructions": w.get("customInstructions"),
    }
    if w.get("stats"):
        out["stats"] = {snake(k): v for k, v in w["stats"].items()}
    return compact(out)


def snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_" + ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


def snake_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {snake(k): snake_keys(loads(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [snake_keys(v) for v in obj]
    return obj
