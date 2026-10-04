"""Strobes Agents MCP server — AI workspaces, their outputs & workflows.

Wraps ``strobes_gql_client`` against the Strobes public GraphQL endpoint
(MasterKey auth) to:
  * manage AI workspaces,
  * spin up / stop agent tasks,
  * read what agents produced (findings, assets, files),
  * view & send chat messages,
  * run and control workflows (start from template, pause/resume/cancel).

Companion to ``strobes-mcp`` (assets/findings/engagements).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP

from .client import get_context
from strobes_gql_client.exceptions import GraphQLRequestError

logging.basicConfig(level=logging.WARNING)
mcp = FastMCP("strobes-agents")

_WS_FIELDS = ("id", "name", "status", "workspace_type", "template_slug",
              "engagement_name", "created_by_email", "created_at")
_TASK_FIELDS = ("id", "title", "task_type", "agent_type", "status",
                "error_message", "created_at", "started_at", "completed_at",
                "duration_seconds")
_MAX_INLINE_FILE = 200_000  # bytes of file content we inline in get_workspace_file


def _pick(obj, fields):
    if not isinstance(obj, dict):
        return obj
    return {k: obj.get(k) for k in fields if k in obj}


def _guard(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except GraphQLRequestError as e:
            msgs = [x.get("message", str(x)) for x in (e.errors or [])]
            return {"error": "; ".join(msgs) or str(e), "operation": e.operation_name}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ------------------------------- connectivity -------------------------------
@mcp.tool()
@_guard
def strobes_agents_whoami() -> dict:
    """Verify connectivity and show the configured host/org plus workspace and
    agent counts. Call this first."""
    client, cfg = get_context()
    ws = client.execute_query("workspaces", organization_id=cfg.organization_id,
                              limit=1)["data"]["workspaces"]
    agents = client.execute_query("available_agents",
                                  organization_id=cfg.organization_id
                                  )["data"]["availableAgents"]
    return {"host": cfg.host, "organization_id": cfg.organization_id,
            "graphql_url": cfg.public_graphql_url, "connected": True,
            "has_workspaces": bool(ws), "available_agent_count": len(agents)}


@mcp.tool()
@_guard
def list_agents() -> dict:
    """List the AI agents available in this organization (system + custom).
    Use an agent's `id` as `agent_type` when spinning up a task."""
    client, cfg = get_context()
    agents = client.execute_query("available_agents",
                                  organization_id=cfg.organization_id
                                  )["data"]["availableAgents"]
    return {"agents": [_pick(a, ("id", "name", "description", "role", "source"))
                       for a in agents], "count": len(agents)}


# ------------------------------- workspaces --------------------------------
@mcp.tool()
@_guard
def list_workspaces(status: Optional[str] = None,
                    engagement_id: Optional[str] = None,
                    limit: int = 25, offset: int = 0) -> dict:
    """List AI workspaces. status: 'active', 'initializing' or 'archived'."""
    client, cfg = get_context()
    kwargs: dict[str, Any] = {"organization_id": cfg.organization_id,
                              "limit": limit, "offset": offset}
    if status is not None:
        kwargs["status"] = status
    if engagement_id is not None:
        kwargs["engagement_id"] = engagement_id
    ws = client.execute_query("workspaces", **kwargs)["data"]["workspaces"]
    return {"workspaces": [_pick(w, _WS_FIELDS) for w in ws], "count": len(ws)}


@mcp.tool()
@_guard
def get_workspace(workspace_id: str) -> dict:
    """Get a single workspace with its stats."""
    client, _ = get_context()
    w = client.execute_query("workspace", id=workspace_id)["data"]["workspace"]
    out = _pick(w or {}, _WS_FIELDS + ("description", "custom_instructions",
                                       "supervisor_mode"))
    if isinstance(w, dict) and w.get("stats"):
        out["stats"] = w["stats"]
    return {"workspace": out}


@mcp.tool()
@_guard
def workspace_stats(workspace_id: str) -> dict:
    """Get statistics for a workspace (chat/task/asset/finding counts, credits)."""
    client, _ = get_context()
    s = client.execute_query("workspace_stats", workspace_id=workspace_id)
    return {"stats": s["data"]["workspaceStats"]}


@mcp.tool()
@_guard
def create_workspace(name: str, engagement_id: Optional[str] = None,
                     settings: Optional[dict] = None) -> dict:
    """Create a new (empty) AI workspace. To create one that runs a predefined
    workflow, use `start_workflow` instead."""
    client, cfg = get_context()
    kwargs: dict[str, Any] = {"organization_id": cfg.organization_id, "name": name}
    if engagement_id is not None:
        kwargs["engagement_id"] = engagement_id
    if settings is not None:
        kwargs["settings"] = settings
    p = client.execute_mutation("create_workspace", **kwargs)
    return {"workspace": _pick((p or {}).get("workspace", {}), _WS_FIELDS),
            "setup_thread_id": ((p or {}).get("setupThread") or {}).get("id")}


@mcp.tool()
@_guard
def archive_workspace(workspace_id: str) -> dict:
    """Archive a workspace (soft delete)."""
    client, _ = get_context()
    p = client.execute_mutation("archive_workspace", workspace_id=workspace_id)
    return {"ok": (p or {}).get("success"),
            "workspace": _pick((p or {}).get("workspace", {}), ("id", "status"))}


# ----------------------------- agent tasks ---------------------------------
@mcp.tool()
@_guard
def list_workspace_tasks(workspace_id: str, status: Optional[str] = None) -> dict:
    """List agent tasks in a workspace (QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED)."""
    client, _ = get_context()
    kwargs: dict[str, Any] = {"workspace_id": workspace_id}
    if status is not None:
        kwargs["status"] = status
    t = client.execute_query("workspace_tasks", **kwargs)["data"]["workspaceTasks"]
    return {"tasks": [_pick(x, _TASK_FIELDS) for x in t], "count": len(t)}


@mcp.tool()
@_guard
def create_workspace_task(workspace_id: str, title: str, instructions: str,
                          agent_type: str, task_type: str = "custom",
                          input_data: Optional[dict] = None) -> dict:
    """Spin up (create & queue) an agent task in an ACTIVE workspace.

    agent_type: an agent id from `list_agents` (e.g. "web_pentest_agent").
    instructions: the natural-language task. Runs asynchronously — poll
    `list_workspace_tasks` for status.
    """
    client, _ = get_context()
    kwargs: dict[str, Any] = {"workspace_id": workspace_id, "title": title,
                              "instructions": instructions,
                              "agent_type": agent_type, "task_type": task_type}
    if input_data is not None:
        kwargs["input_data"] = input_data
    p = client.execute_mutation("create_workspace_task", **kwargs)
    return {"task": _pick((p or {}).get("task", {}), _TASK_FIELDS)}


@mcp.tool()
@_guard
def cancel_workspace_task(workspace_id: str, task_id: str) -> dict:
    """Stop (cancel) a running or queued agent task."""
    client, _ = get_context()
    p = client.execute_mutation("cancel_workspace_task",
                                workspace_id=workspace_id, task_id=task_id)
    return {"ok": (p or {}).get("success"),
            "task": _pick((p or {}).get("task", {}), ("id", "status"))}


@mcp.tool()
@_guard
def retry_workspace_task(workspace_id: str, task_id: str) -> dict:
    """Retry a failed or cancelled agent task."""
    client, _ = get_context()
    p = client.execute_mutation("retry_workspace_task",
                                workspace_id=workspace_id, task_id=task_id)
    return {"ok": (p or {}).get("success"),
            "task": _pick((p or {}).get("task", {}), ("id", "status"))}


# --------------------------- workspace outputs -----------------------------
@mcp.tool()
@_guard
def list_workspace_findings(workspace_id: str, page: int = 1, page_size: int = 25,
                            search: Optional[str] = None) -> dict:
    """List security findings produced in a workspace."""
    client, _ = get_context()
    kwargs: dict[str, Any] = {"workspace_id": workspace_id, "page": page,
                              "page_size": page_size}
    if search is not None:
        kwargs["search"] = search
    f = client.execute_query("workspace_findings", **kwargs
                             )["data"]["workspaceFindings"]
    return {"findings": f, "count": len(f)}


@mcp.tool()
@_guard
def list_workspace_assets(workspace_id: str, page: int = 1, page_size: int = 25,
                          search: Optional[str] = None) -> dict:
    """List assets discovered / linked in a workspace."""
    client, _ = get_context()
    kwargs: dict[str, Any] = {"workspace_id": workspace_id, "page": page,
                              "page_size": page_size}
    if search is not None:
        kwargs["search"] = search
    a = client.execute_query("workspace_assets", **kwargs)["data"]["workspaceAssets"]
    return {"assets": a, "count": len(a)}


@mcp.tool()
@_guard
def list_workspace_files(workspace_id: str, path: str = "",
                         recursive: bool = False,
                         search: Optional[str] = None) -> dict:
    """Browse files/folders an agent wrote in a workspace. path scopes to a
    subfolder (e.g. 'reports/'); recursive lists the whole tree."""
    client, _ = get_context()
    kwargs: dict[str, Any] = {"workspace_id": workspace_id, "path": path,
                              "recursive": recursive}
    if search is not None:
        kwargs["search"] = search
    f = client.execute_query("workspace_files", **kwargs)["data"]["workspaceFiles"]
    return {"files": f, "count": len(f)}


@mcp.tool()
@_guard
def get_workspace_file(workspace_id: str, path: str) -> dict:
    """Pull a single workspace file: returns a presigned download URL and, for
    small text files, the inlined content."""
    client, cfg = get_context()
    url = client.execute_query("workspace_file_download_url",
                               workspace_id=workspace_id, path=path
                               )["data"]["workspaceFileDownloadUrl"]
    out: dict[str, Any] = {"path": path, "download_url": url}
    if url:
        try:
            r = requests.get(url, timeout=30, verify=cfg.verify_tls,
                             stream=True)
            r.raise_for_status()
            chunk = r.raw.read(_MAX_INLINE_FILE + 1, decode_content=True)
            if len(chunk) <= _MAX_INLINE_FILE:
                try:
                    out["content"] = chunk.decode("utf-8")
                except UnicodeDecodeError:
                    out["note"] = "binary file — use download_url"
            else:
                out["note"] = "file too large to inline — use download_url"
        except Exception as e:  # noqa: BLE001
            out["note"] = f"could not fetch content ({e}); use download_url"
    return out


@mcp.tool()
@_guard
def download_workspace(workspace_id: str, path: str = "") -> dict:
    """Get a zip download URL for a whole workspace (or a folder within it)."""
    client, _ = get_context()
    url = client.execute_query("workspace_download_url", workspace_id=workspace_id,
                               path=path)["data"]["workspaceDownloadUrl"]
    return {"download_url": url, "path": path or "(entire workspace)"}


# --------------------------- task conversation -----------------------------
@mcp.tool()
@_guard
def get_task_messages(workspace_id: str, task_id: str, limit: int = 50,
                      offset: int = 0) -> dict:
    """Read the conversation/messages an agent produced while running a task.

    Paginated newest-first: offset=0 is the latest page, offset=limit the page
    before it (a "load older" control). Returns [] until the task's run starts.
    """
    client, _ = get_context()
    m = client.execute_query("workspace_task_messages", workspace_id=workspace_id,
                             task_id=task_id, limit=limit, offset=offset
                             )["data"]["workspaceTaskMessages"]
    return {"messages": m, "count": len(m)}


# ------------------------------ workflows ----------------------------------
@mcp.tool()
@_guard
def list_workflow_templates() -> dict:
    """List available workflow templates (slug + required variables) that
    `start_workflow` can instantiate."""
    client, _ = get_context()
    t = client.execute_query("workflow_templates")["data"]["workflowTemplates"]
    return {"templates": t, "count": len(t)}


@mcp.tool()
@_guard
def get_workspace_workflow(workspace_id: str) -> dict:
    """Get the current workflow instance for a workspace (status, current phase,
    phase list, task progress). Returns null if the workspace has no workflow."""
    client, _ = get_context()
    wf = client.execute_query("workspace_workflow", workspace_id=workspace_id
                             )["data"]["workspaceWorkflow"]
    return {"workflow": wf}


@mcp.tool()
@_guard
def start_workflow(template_slug: str, variables: dict, name: Optional[str] = None,
                   engagement_id: Optional[str] = None) -> dict:
    """Start a workflow: create a workspace from a template and kick off its
    multi-phase agent pipeline.

    template_slug: from `list_workflow_templates`. variables: the template's
    required variables (see each template's `required_variables`).
    """
    client, cfg = get_context()
    kwargs: dict[str, Any] = {"organization_id": cfg.organization_id,
                              "template_slug": template_slug,
                              "variables": variables}
    if name is not None:
        kwargs["name"] = name
    if engagement_id is not None:
        kwargs["engagement_id"] = engagement_id
    p = client.execute_mutation("create_workspace_from_template", **kwargs)
    return {"workspace": _pick((p or {}).get("workspace", {}), _WS_FIELDS),
            "setup_thread_id": ((p or {}).get("setupThread") or {}).get("id")}


@mcp.tool()
@_guard
def pause_workflow(workspace_id: str) -> dict:
    """Pause a running workflow in a workspace."""
    client, _ = get_context()
    p = client.execute_mutation("pause_workflow", workspace_id=workspace_id)
    return {"ok": (p or {}).get("success")}


@mcp.tool()
@_guard
def resume_workflow(workspace_id: str) -> dict:
    """Resume a paused workflow in a workspace."""
    client, _ = get_context()
    p = client.execute_mutation("resume_workflow", workspace_id=workspace_id)
    return {"ok": (p or {}).get("success")}


@mcp.tool()
@_guard
def cancel_workflow(workspace_id: str) -> dict:
    """Cancel a running or paused workflow in a workspace."""
    client, _ = get_context()
    p = client.execute_mutation("cancel_workflow", workspace_id=workspace_id)
    return {"ok": (p or {}).get("success")}


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
