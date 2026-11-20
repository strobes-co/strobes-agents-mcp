"""Strobes Agents MCP server.

Drive Strobes AI agents from any MCP host: sign in through the browser, spin up
agent tasks in AI workspaces and follow them to completion, read what agents
produced (findings, assets, files), and run multi-phase workflows.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__, auth, render
from . import queries as Q
from .api import ENV_INCOMPLETE, NOT_SIGNED_IN, AuthError, StrobesAPI, StrobesError
from .config import (ConfigError, Profile, clear_login, list_profiles, resolve_profile,
                     set_current_profile)

log = logging.getLogger("strobes_agents_mcp")
_sleep = asyncio.sleep  # indirection so tests can fast-forward polling

INSTRUCTIONS = """\
Strobes AI agents: spin up security agents (pentest, triage, recon, reporting …)
in AI workspaces, follow their work, and collect what they produce.

Start with `strobes_whoami`. If it says you're not signed in, call `strobes_login`,
show the user the link and code, then call `strobes_login_status` until approved.

Model: a **workspace** holds **tasks** (one unit of agent work each). To get an
agent to do something, `create_task` with plain-language instructions and an agent
id from `list_agents`; tasks run asynchronously in Strobes' cloud — use
`wait_for_task` (or `get_task`) to follow progress and read the result. Outputs
land in the workspace: `list_findings`, `list_assets`, `list_files`/`read_file`.
For a multi-phase pipeline, `list_workflow_templates` then `start_workflow`.

`workspace_id` defaults to the workspace chosen at sign-in, when there is one.
Agent runs consume AI credits — don't create duplicate tasks, and don't retry a
task that failed for budget/credit reasons without asking the user.
"""

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                     openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                        openWorldHint=True)
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                   idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                              idempotentHint=True, openWorldHint=True)

WorkspaceId = Annotated[Optional[str], Field(
    description="Workspace UUID. Omit to use the workspace chosen at sign-in.")]
TaskId = Annotated[str, Field(description="Task UUID (from create_task / list_tasks).")]
TaskStatus = Literal["queued", "running", "blocked", "completed", "failed", "cancelled"]


# --------------------------------------------------------------------------
# session state
# --------------------------------------------------------------------------
class Session:
    """Per-process credentials, API clients and the in-flight browser login."""

    def __init__(self) -> None:
        self.profile_override: str | None = None
        self.login: auth.DeviceLogin | None = None
        self.login_task: asyncio.Task | None = None
        # Clients are shared by concurrent requests, so they are never closed while
        # the process runs (closing one could break another request mid-call); the
        # cache is bounded and evicted clients are just dropped.
        self._clients: OrderedDict[tuple, StrobesAPI] = OrderedDict()

    def profile(self, ctx: Context | None = None) -> Profile:
        hdr = _request_profile(ctx)
        if hdr is not None:
            return hdr
        if os.getenv("STROBES_MCP_REQUIRE_HEADER_AUTH"):
            raise AuthError("This server needs per-request credentials: send "
                            "`Authorization: Bearer <Strobes MasterKey>` plus "
                            "`X-Strobes-Url` and `X-Strobes-Organization-Id` headers.")
        return resolve_profile(self.profile_override)

    def api(self, ctx: Context | None = None) -> StrobesAPI:
        p = self.profile(ctx)
        if not p.complete:
            msg = ENV_INCOMPLETE if p.source == "env-incomplete" else NOT_SIGNED_IN
            if self.login and self.login.status == "pending":
                msg = ("Sign-in is in progress — ask the user to approve it at "
                       f"{self.login.verification_url} (code {self.login.user_code}), "
                       "then call `strobes_login_status`.")
            raise AuthError(msg)
        key = (p.graphql_url, p.master_key, p.org_id, p.verify_tls, p.tenant_host)
        client = self._clients.get(key)
        if client is None or client.closed:
            client = StrobesAPI(p)
            self._clients[key] = client
            while len(self._clients) > MAX_CLIENTS:
                _retire(self._clients.popitem(last=False)[1])
        else:
            self._clients.move_to_end(key)
        return client

    def reset_clients(self) -> None:
        """Forget cached clients (after sign-in/out). In-flight calls keep theirs."""
        old, self._clients = self._clients, OrderedDict()
        for c in old.values():
            _retire(c)


MAX_CLIENTS = 64
RETIRE_GRACE_SECONDS = 1800  # longer than any single tool call (max wait is 1800s)


def _retire(client: StrobesAPI) -> None:
    """Close a dropped client later, once calls still using it have finished."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(RETIRE_GRACE_SECONDS,
                    lambda: loop.create_task(client.aclose()) if not loop.is_closed() else None)
SESSION = Session()


def _http_request(ctx: Context | None) -> Any:
    """The HTTP request behind this tool call, or None over stdio / in-memory."""
    if ctx is None:
        return None
    try:
        return ctx.request_context.request
    except (AttributeError, ValueError, LookupError):
        return None


def _local_only(ctx: Context | None, what: str) -> None:
    """Refuse host-affecting operations to remote (HTTP) clients.

    Over HTTP the stored login belongs to the machine running the server, not to
    the caller — a remote client must not sign it in/out, switch it, or read files
    from its disk.
    """
    if os.getenv("STROBES_MCP_LOCAL_HTTP") and not os.getenv("STROBES_MCP_REQUIRE_HEADER_AUTH"):
        return
    if _http_request(ctx) is not None or os.getenv("STROBES_MCP_TRANSPORT", "stdio") != "stdio":
        raise ToolError(f"{what} is only available when the server runs locally over stdio. "
                        "Remote clients authenticate per request (Authorization: Bearer "
                        "<MasterKey>); sign the host in with `strobes-agents-mcp login`.")


def _env_only_profile() -> Profile:
    """Deployment defaults from env vars only (no stored login)."""
    from .config import _env, _env_base_url, _env_bool

    p = Profile(base_url=_env_base_url() or "",
                org_id=_env("STROBES_ORGANIZATION_ID", "STROBES_ORG_ID", "STROBES_AI_ORG_ID") or "",
                deployment=_env("STROBES_AI_DEPLOYMENT") or "",
                tenant_host=_env("STROBES_TENANT_HOST"))
    tls = _env_bool("STROBES_VERIFY_TLS")
    p.verify_tls = False if _env_bool("STROBES_INSECURE") else (True if tls is None else tls)
    return p


def _allowed_url(url: str, server: Profile) -> bool:
    """Which Strobes URLs a remote client may point this server at (anti-SSRF)."""
    from fnmatch import fnmatch
    from urllib.parse import urlparse

    u = urlparse(url if "://" in url else f"https://{url}")
    host = (u.hostname or "").lower()
    if not host:
        return False
    patterns = [x.strip().lower() for x in os.getenv(
        "STROBES_MCP_ALLOWED_URLS", "*.strobes.co,strobes.co").split(",") if x.strip()]
    if server.host:
        patterns.append(server.host.lower())
    if u.scheme != "https" and host != (server.host or "").lower():
        return False
    return any(fnmatch(host, pat) for pat in patterns)


def _request_profile(ctx: Context | None) -> Profile | None:
    """Credentials sent by a remote client as HTTP headers (streamable-http mode).

    Accepted: ``Authorization: Bearer <MasterKey>`` (or ``X-Strobes-Api-Key``),
    ``X-Strobes-Organization-Id`` and ``X-Strobes-Url``. Missing pieces fall back
    to the server's own configuration.
    """
    req = _http_request(ctx)
    headers = getattr(req, "headers", None)
    if not headers:
        return None
    key = headers.get("x-strobes-api-key")
    authz = headers.get("authorization") or ""
    if not key and authz.lower().startswith(("bearer ", "token ")):
        key = authz.split(None, 1)[1].strip()
    if not key:
        return None
    if os.getenv("STROBES_MCP_REQUIRE_HEADER_AUTH"):
        # never expose the host's stored login (URL/org) — only what the operator
        # configured for the deployment via env
        server = _env_only_profile()
    else:
        server = resolve_profile(SESSION.profile_override)
    url = (headers.get("x-strobes-url") or "").strip()
    if url:
        if not _allowed_url(url, server):
            raise AuthError(f"X-Strobes-Url {url!r} isn't an allowed Strobes URL for this "
                            "server (https and *.strobes.co by default; the operator can "
                            "extend STROBES_MCP_ALLOWED_URLS).")
        p = Profile(base_url=url, verify_tls=server.verify_tls)
    else:
        # single-tenant deployment: the operator configured the URL
        p = Profile(base_url=server.base_url, deployment=server.deployment,
                    verify_tls=server.verify_tls, tenant_host=server.tenant_host)
    p.name = "request"
    p.master_key = key
    p.org_id = headers.get("x-strobes-organization-id") or (server.org_id if not url else "")
    p.workspace_id = headers.get("x-strobes-workspace-id") or None
    p.source = "request"
    return p


def _ws(ctx: Context | None, workspace_id: str | None) -> str:
    if workspace_id:
        return workspace_id
    ws = SESSION.profile(ctx).workspace_id
    if not ws:
        raise ToolError("workspace_id is required (no default workspace was chosen at "
                        "sign-in). Use `list_workspaces` to find one.")
    return ws


def tool(annotations: ToolAnnotations, title: str | None = None):
    """Register an async tool; turn API failures into clean MCP tool errors."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
                if isinstance(result, dict):
                    # compact JSON text (the SDK default pretty-prints, ~30% more
                    # tokens) plus the same data as structuredContent
                    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"),
                                      default=str)
                    return CallToolResult(content=[TextContent(type="text", text=text)],
                                          structuredContent=result)
                return result
            except ToolError:
                raise
            except (StrobesError, ConfigError, auth.LoginError) as e:
                raise ToolError(str(e)) from e
            except httpx.HTTPError as e:
                raise ToolError(f"Network error talking to Strobes: {e}") from e

        return mcp.tool(annotations=annotations, title=title)(wrapper)

    return deco


mcp = FastMCP("strobes-agents", instructions=INSTRUCTIONS, website_url="https://strobes.co")
mcp._mcp_server.version = __version__  # report our version, not the SDK's


# --------------------------------------------------------------------------
# sign-in & session
# --------------------------------------------------------------------------
async def _poll_login_forever(login: auth.DeviceLogin) -> None:
    await auth.wait_for_login(login)  # never raises; failures land in login.status
    if login.status == "approved":
        # use the profile just signed in to, even if it isn't the file's default
        SESSION.profile_override = login.profile
        SESSION.reset_clients()


@tool(WRITE, "Sign in to Strobes")
async def strobes_login(
    ctx: Context,
    url: Annotated[Optional[str], Field(
        description="Strobes URL, e.g. https://acme.strobes.co. Defaults to the URL you're "
                    "signed in to, else https://app.strobes.co (org is picked in the browser).")] = None,
    profile: Annotated[Optional[str], Field(
        description="Profile name to save the sign-in under (default: current profile).")] = None,
    web_url: Annotated[Optional[str], Field(
        description="Web app URL for the approval page if it differs from the API host "
                    "(local dev only, e.g. http://localhost:3000).")] = None,
    open_browser: Annotated[bool, Field(
        description="Open the sign-in page in the local browser.")] = True,
) -> dict[str, Any]:
    """Start a browser sign-in (device flow, like `strobes login`).

    Returns a link and a short code. Show both to the user and ask them to approve
    in the browser (they pick the organization and an optional default
    workspace), then call `strobes_login_status`. Credentials are saved to the
    profile store shared with the Strobes CLI.
    """
    _local_only(ctx, "Signing in")
    current = resolve_profile(profile or SESSION.profile_override)
    name = profile or current.name
    if SESSION.login_task and not SESSION.login_task.done():
        SESSION.login_task.cancel()
    fallback = current if current.base_url else resolve_profile(SESSION.profile_override)
    default_url = current.login_url or (fallback.web_url if fallback.base_url else None)
    login = await auth.start_login(url or default_url, profile=name,
                                   verify_tls=fallback.verify_tls, web_url=web_url,
                                   launch_browser=open_browser)
    SESSION.login = login
    SESSION.login_task = asyncio.create_task(_poll_login_forever(login))
    out = login.public()
    out["next_step"] = (
        f"Ask the user to open {login.verification_url}, check the code "
        f"{login.user_code} matches, pick the organization and click Authorize. "
        "Then call strobes_login_status.")
    return out


@tool(RO, "Check sign-in progress")
async def strobes_login_status(
    ctx: Context,
    wait_seconds: Annotated[int, Field(ge=0, le=300, description=(
        "Wait up to this long for the user to finish approving."))] = 60,
) -> dict[str, Any]:
    """Status of the browser sign-in started by `strobes_login`
    (pending / approved / denied / expired)."""
    _local_only(ctx, "Sign-in status")
    login = SESSION.login
    if login is None:
        p = SESSION.profile(ctx)
        return {"status": "signed_in" if p.complete else "not_started",
                **({"profile": p.name, "email": p.email} if p.complete else {})}
    deadline = time.monotonic() + wait_seconds
    while login.status == "pending" and time.monotonic() < deadline:
        left = int(deadline - time.monotonic())
        await ctx.report_progress(wait_seconds - left, wait_seconds,
                                  f"Waiting for approval (code {login.user_code})")
        await asyncio.sleep(min(2.0, max(0.2, deadline - time.monotonic())))
    out = login.public()
    if login.status == "pending":
        out["next_step"] = "Still waiting for the user — call strobes_login_status again."
    elif login.status == "approved":
        out["next_step"] = "Signed in. Call strobes_whoami or list_workspaces."
    return out


@tool(RO, "Who am I / connection status")
async def strobes_whoami(ctx: Context) -> dict[str, Any]:
    """Show the active Strobes connection (URL, organization, user, default
    workspace) and verify it works. Call this first."""
    p = SESSION.profile(ctx)
    remote = p.source == "request" or _http_request(ctx) is not None
    out: dict[str, Any] = {"server_version": __version__, **p.redacted()}
    if remote:
        out.pop("profile", None)
    if not p.complete:
        out.update(signed_in=False, next_step=NOT_SIGNED_IN)
        if not remote:
            out["profiles"] = list_profiles()
        return render.compact(out)
    api = SESSION.api(ctx)
    try:
        data = await api.graphql(Q.WORKSPACES, {"org": p.org_id, "limit": 1}, op="workspaces")
        out.update(signed_in=True, connected=True,
                   has_workspaces=bool(data.get("workspaces")))
    except AuthError as e:
        out.update(signed_in=False, connected=False, error=str(e))
    if p.workspace_id:
        try:
            w = (await api.graphql(Q.WORKSPACE_STATUS, {"id": p.workspace_id}))["workspace"]
            out["default_workspace"] = render.workspace(w) if w else None
        except StrobesError:
            pass
    profiles = [] if remote else list_profiles()
    if len(profiles) > 1:
        out["profiles"] = profiles
    return render.compact(out)


@tool(WRITE_IDEMPOTENT, "Switch Strobes profile")
async def strobes_use_profile(
    ctx: Context,
    profile: Annotated[str, Field(description="Profile name (see strobes_whoami → profiles).")],
    persist: Annotated[bool, Field(description="Also make it the default for the CLI and "
                                               "future sessions.")] = False,
) -> dict[str, Any]:
    """Switch to another signed-in Strobes organization/profile."""
    _local_only(ctx, "Switching profiles")
    names = {p["profile"] for p in list_profiles()}
    if profile not in names:
        raise ToolError(f"No profile named {profile!r}. Known: {', '.join(sorted(names)) or 'none'}. "
                        "Use strobes_login with profile=... to add one.")
    if persist:
        set_current_profile(profile)
    SESSION.profile_override = profile
    return resolve_profile(profile).redacted()


@tool(DESTRUCTIVE, "Sign out of Strobes")
async def strobes_logout(
    ctx: Context,
    profile: Annotated[Optional[str], Field(description="Profile to sign out (default: current).")] = None,
) -> dict[str, Any]:
    """Forget the stored API key for a profile (also signs the Strobes CLI out of it).
    The key itself stays valid on the server; revoke it in Settings → API access."""
    _local_only(ctx, "Signing out")
    name = profile or resolve_profile(SESSION.profile_override).name
    removed = clear_login(name)
    SESSION.reset_clients()
    return {"profile": name, "signed_out": removed}


# --------------------------------------------------------------------------
# agents & workspaces
# --------------------------------------------------------------------------
@tool(RO, "List AI agents")
async def list_agents(
    ctx: Context,
    search: Annotated[Optional[str], Field(description="Filter by id/name/description text.")] = None,
) -> dict[str, Any]:
    """List the AI agents available in this organization (system + custom).
    Pass an agent's `id` as `agent` to `create_task`."""
    api = SESSION.api(ctx)
    agents = (await api.graphql(Q.AGENTS, {"org": api.profile.org_id},
                                op="availableAgents"))["availableAgents"] or []
    rows = [render.compact({"id": a.get("id"), "name": a.get("name"),
                            "description": render.truncate(a.get("description") or "", 240),
                            "role": a.get("role"), "source": lower_or(a.get("source"))})
            for a in agents]
    if search:
        s = search.lower()
        rows = [r for r in rows if s in " ".join(str(v) for v in r.values()).lower()]
    return {"agents": rows, "count": len(rows)}


def lower_or(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


@tool(RO, "List workspaces")
async def list_workspaces(
    ctx: Context,
    status: Annotated[Optional[Literal["active", "initializing", "archived"]],
                      Field(description="Filter by status.")] = None,
    search: Annotated[Optional[str], Field(description="Case-insensitive name filter.")] = None,
    engagement_id: Annotated[Optional[str], Field(description="Only workspaces linked to this engagement.")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """List AI workspaces, newest first."""
    api = SESSION.api(ctx)
    fetch = 200 if search else limit
    ws = (await api.graphql(Q.WORKSPACES, {
        "org": api.profile.org_id, "status": status, "engagement": engagement_id,
        "limit": fetch, "offset": offset}, op="workspaces"))["workspaces"] or []
    if search:
        ws = [w for w in ws if search.lower() in (w.get("name") or "").lower()][:limit]
    rows = [render.workspace(w) for w in ws]
    for r in rows:
        r.pop("description", None)
    out: dict[str, Any] = {"workspaces": rows, "count": len(rows)}
    if not search and len(ws) == limit:
        out["next_offset"] = offset + limit
    return out


@tool(RO, "Get a workspace")
async def get_workspace(ctx: Context, workspace_id: WorkspaceId = None) -> dict[str, Any]:
    """Get one workspace with its stats (tasks, findings, assets, files, credits used)."""
    api = SESSION.api(ctx)
    w = (await api.graphql(Q.WORKSPACE_GET, {"id": _ws(ctx, workspace_id)},
                           op="workspace"))["workspace"]
    if not w:
        raise ToolError("Workspace not found.")
    out = render.workspace(w)
    out["web_url"] = api.profile.app_url(f"ai/workspaces/{w['id']}")
    return out


async def _activate(api: StrobesAPI, workspace_id: str) -> str:
    """Finish an `initializing` workspace's setup so it accepts tasks / starts
    its workflow. Returns the resulting status."""
    try:
        d = await api.graphql(Q.SKIP_SETUP, {"ws": workspace_id}, op="skipWorkspaceSetup")
        return lower_or(((d.get("skipWorkspaceSetup") or {}).get("workspace") or {})
                        .get("status")) or "active"
    except AuthError:
        pass  # older server: mutation not exposed to API keys
    except StrobesError as e:
        if "not in initializing" in str(e):
            return "active"
        if "Cannot query field" not in str(e) and "isn't available" not in str(e):
            raise
    # Fallback for servers without skipWorkspaceSetup on the public API: opening
    # a chat through the agents REST API activates an initializing workspace.
    await api.rest("POST", "cli/threads/create/",
                   json={"workspace_id": workspace_id, "title": "MCP session"})
    w = (await api.graphql(Q.WORKSPACE_STATUS, {"id": workspace_id}))["workspace"] or {}
    return lower_or(w.get("status")) or "unknown"


@tool(WRITE, "Create a workspace")
async def create_workspace(
    ctx: Context,
    name: Annotated[str, Field(description="Workspace name, e.g. 'Acme web pentest'.")],
    engagement_id: Annotated[Optional[str], Field(description="Link to an existing engagement.")] = None,
    activate: Annotated[bool, Field(description=(
        "Skip the interactive setup chat so the workspace is ready for tasks right "
        "away. Set false to leave it initializing for setup in the web app."))] = True,
) -> dict[str, Any]:
    """Create an AI workspace to hold agent tasks and their outputs. To create a
    workspace that runs a predefined multi-phase pipeline use `start_workflow`."""
    api = SESSION.api(ctx)
    p = (await api.graphql(Q.CREATE_WORKSPACE, {"org": api.profile.org_id, "name": name,
                                                "engagement": engagement_id},
                           op="createWorkspace"))["createWorkspace"] or {}
    w = render.workspace(p.get("workspace"))
    if activate and w.get("id") and w.get("status") != "active":
        w["status"] = await _activate(api, w["id"])
    w["web_url"] = api.profile.app_url(f"ai/workspaces/{w.get('id')}")
    return w


@tool(WRITE_IDEMPOTENT, "Activate a workspace")
async def activate_workspace(ctx: Context, workspace_id: WorkspaceId = None) -> dict[str, Any]:
    """Finish setup of an `initializing` workspace (like "Skip setup" in the web app)
    so it accepts tasks; also starts a workflow waiting on setup."""
    api = SESSION.api(ctx)
    ws = _ws(ctx, workspace_id)
    return {"workspace_id": ws, "status": await _activate(api, ws)}


@tool(DESTRUCTIVE, "Archive a workspace")
async def archive_workspace(ctx: Context, workspace_id: Annotated[str, Field(
        description="Workspace UUID to archive.")]) -> dict[str, Any]:
    """Archive (soft-delete) a workspace. Its data is kept but it leaves active lists."""
    api = SESSION.api(ctx)
    p = (await api.graphql(Q.ARCHIVE_WORKSPACE, {"ws": workspace_id},
                           op="archiveWorkspace"))["archiveWorkspace"] or {}
    return {"ok": bool(p.get("success")), "workspace": render.workspace(p.get("workspace"))}


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------
@tool(RO, "List tasks")
async def list_tasks(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    status: Annotated[Optional[TaskStatus], Field(description="Filter by status.")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """List agent tasks in a workspace, newest first."""
    api = SESSION.api(ctx)
    tasks = (await api.graphql(Q.TASKS, {"ws": _ws(ctx, workspace_id), "status": status},
                               op="workspaceTasks"))["workspaceTasks"] or []
    rows = [render.task(t) for t in tasks]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    return {"tasks": rows[:limit], "count": len(rows), "by_status": counts}


async def _find_task(api: StrobesAPI, ws: str, task_id: str, *,
                     light: bool = False) -> dict[str, Any]:
    doc = Q.TASKS if light else Q.TASKS_WITH_DATA
    tasks = (await api.graphql(doc, {"ws": ws}, op="workspaceTasks"))["workspaceTasks"] or []
    for t in tasks:
        if t.get("id") == task_id:
            return t
    raise ToolError(f"Task {task_id} not found in workspace {ws}.")


async def _messages(api: StrobesAPI, ws: str, task_id: str, limit: int,
                    offset: int = 0) -> list[dict[str, Any]]:
    return (await api.graphql(Q.TASK_MESSAGES, {"ws": ws, "task": task_id, "limit": limit,
                                                "offset": offset},
                              op="workspaceTaskMessages"))["workspaceTaskMessages"] or []


async def _task_view(api: StrobesAPI, ws: str, task_id: str, *,
                     with_result: bool = True) -> dict[str, Any]:
    t = await _find_task(api, ws, task_id)
    out = render.task(t, with_instructions=True)
    out["workspace_id"] = ws
    if with_result and out.get("status") not in ("queued",):
        try:
            msgs = await _messages(api, ws, task_id, limit=5)
        except StrobesError:
            msgs = []
        answer = render.final_answer(msgs)
        if answer:
            key = "result" if out.get("status") in render.TERMINAL_TASK else "latest_update"
            out[key] = answer
    return out


@tool(WRITE, "Spin up an agent task")
async def create_task(
    ctx: Context,
    instructions: Annotated[str, Field(description=(
        "What the agent should do, in plain language. Include targets, scope, "
        "credentials to use and what to deliver."))],
    agent: Annotated[str, Field(description=(
        "Agent id from list_agents, e.g. 'web_pentest_agent', 'general_security_assistant'."))],
    workspace_id: WorkspaceId = None,
    title: Annotated[Optional[str], Field(description="Short task title (defaults to the "
                                                      "start of the instructions).")] = None,
    task_type: Annotated[Literal["custom", "browse", "enumerate", "test", "validate"],
                         Field(description="Task category.")] = "custom",
    wait_seconds: Annotated[int, Field(ge=0, le=1800, description=(
        "Block up to this long for the task to finish and return its result. 0 = "
        "return immediately (then use wait_for_task)."))] = 0,
    input_data: Annotated[Optional[dict[str, Any]], Field(description=(
        "Advanced: structured task input, e.g. {\"llm_model\": <model id>} to override "
        "the workspace's default model."))] = None,
) -> dict[str, Any]:
    """Spin up an agent: create and queue a task in an active workspace.

    Runs asynchronously in Strobes' cloud and consumes AI credits. Returns the
    task id; follow it with `wait_for_task` / `get_task`, read the full exchange
    with `get_task_messages`, stop it with `cancel_task`.
    """
    api = SESSION.api(ctx)
    ws = _ws(ctx, workspace_id)
    title = title or render.truncate(" ".join(instructions.split()), 80)
    variables = {"ws": ws, "title": title, "instructions": instructions,
                 "taskType": task_type, "agentType": agent, "input": input_data}
    try:
        p = (await api.graphql(Q.CREATE_TASK, variables,
                               op="createWorkspaceTask"))["createWorkspaceTask"] or {}
    except StrobesError as e:
        if "initializ" in str(e).lower() or "not active" in str(e).lower():
            raise ToolError(f"{e} — call activate_workspace first.") from e
        raise
    t = render.task(p.get("task") or {})
    t["workspace_id"] = ws
    if wait_seconds and t.get("id"):
        return await _wait(ctx, api, ws, t["id"], wait_seconds, 10)
    t["next_step"] = "Call wait_for_task to follow it to completion."
    return t


@tool(RO, "Get a task")
async def get_task(ctx: Context, task_id: TaskId,
                   workspace_id: WorkspaceId = None) -> dict[str, Any]:
    """Status of one task plus the agent's latest answer (or final result once done)."""
    api = SESSION.api(ctx)
    return await _task_view(api, _ws(ctx, workspace_id), task_id)


async def _wait(ctx: Context, api: StrobesAPI, ws: str, task_id: str, timeout: int,
                poll: int) -> dict[str, Any]:
    start = time.monotonic()
    last = None
    while True:
        t = render.task(await _find_task(api, ws, task_id, light=True))
        status = t.get("status")
        elapsed = int(time.monotonic() - start)
        if status != last:
            log.info("task %s → %s", task_id, status)
            last = status
        if status in render.TERMINAL_TASK:
            break
        if elapsed >= timeout:
            break
        await ctx.report_progress(elapsed, timeout, f"Task {status} ({elapsed}s)")
        await _sleep(min(poll, max(1, timeout - elapsed)))
    out = await _task_view(api, ws, task_id)
    out["waited_seconds"] = int(time.monotonic() - start)
    if out.get("status") not in render.TERMINAL_TASK:
        out["next_step"] = "Still running — call wait_for_task again, or get_task_messages."
    return out


@tool(RO, "Wait for a task to finish")
async def wait_for_task(
    ctx: Context,
    task_id: TaskId,
    workspace_id: WorkspaceId = None,
    timeout_seconds: Annotated[int, Field(ge=5, le=1800, description=(
        "Give up waiting after this long (the task keeps running)."))] = 300,
    poll_seconds: Annotated[int, Field(ge=2, le=120)] = 10,
) -> dict[str, Any]:
    """Wait until a task completes / fails / is cancelled (or the timeout passes),
    reporting progress, then return its status and the agent's result."""
    api = SESSION.api(ctx)
    return await _wait(ctx, api, _ws(ctx, workspace_id), task_id, timeout_seconds, poll_seconds)


@tool(RO, "Read a task's conversation")
async def get_task_messages(
    ctx: Context,
    task_id: TaskId,
    workspace_id: WorkspaceId = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0, description=(
        "Pages walk back in time: 0 = newest page, offset=limit = the page before."))] = 0,
    include_thinking: Annotated[bool, Field(description="Include the agent's reasoning blocks.")] = False,
    max_chars_per_message: Annotated[int, Field(ge=200, le=50000)] = 4000,
    raw: Annotated[bool, Field(description="Return raw message blocks (large).")] = False,
) -> dict[str, Any]:
    """The messages an agent produced while working a task (its prose, plus a
    one-line note per tool call), oldest→newest within the page. Empty until the
    task starts running."""
    api = SESSION.api(ctx)
    msgs = await _messages(api, _ws(ctx, workspace_id), task_id, limit, offset)
    msgs = sorted(msgs, key=lambda m: m.get("createdAt") or "")
    if raw:
        rows = [render.snake_keys(m) for m in msgs]
    else:
        rows = [render.render_message(m, include_thinking=include_thinking,
                                      max_chars=max_chars_per_message) for m in msgs]
    out: dict[str, Any] = {"messages": rows, "count": len(rows)}
    if len(msgs) == limit:
        out["older_offset"] = offset + limit
    return out


@tool(DESTRUCTIVE, "Stop a task")
async def cancel_task(ctx: Context, task_id: TaskId,
                      workspace_id: WorkspaceId = None) -> dict[str, Any]:
    """Stop (cancel) a queued or running agent task."""
    api = SESSION.api(ctx)
    p = (await api.graphql(Q.CANCEL_TASK, {"ws": _ws(ctx, workspace_id), "task": task_id},
                           op="cancelWorkspaceTask"))["cancelWorkspaceTask"] or {}
    return {"ok": bool(p.get("success")), "task": render.task(p.get("task") or {})}


@tool(WRITE, "Retry a task")
async def retry_task(ctx: Context, task_id: TaskId,
                     workspace_id: WorkspaceId = None) -> dict[str, Any]:
    """Re-run a failed or cancelled agent task (consumes AI credits)."""
    api = SESSION.api(ctx)
    p = (await api.graphql(Q.RETRY_TASK, {"ws": _ws(ctx, workspace_id), "task": task_id},
                           op="retryWorkspaceTask"))["retryWorkspaceTask"] or {}
    return {"ok": bool(p.get("success")), "task": render.task(p.get("task") or {})}


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------
SEVERITY = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


@tool(RO, "List findings")
async def list_findings(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    severity: Annotated[Optional[list[Literal["critical", "high", "medium", "low", "info"]]],
                        Field(description="Only these severities.")] = None,
    search: Annotated[Optional[str], Field(description="Text search over titles.")] = None,
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """Security findings (vulnerabilities) produced in a workspace."""
    api = SESSION.api(ctx)
    sev = [SEVERITY[s] for s in severity] if severity else None
    f = (await api.graphql(Q.FINDINGS, {"ws": _ws(ctx, workspace_id), "page": page,
                                        "size": page_size, "search": search, "severity": sev},
                           op="workspaceFindings"))["workspaceFindings"] or []
    rows = [render.compact({"id": x.get("id"), "title": x.get("title"),
                            "severity": lower_or(x.get("severityLabel")) or x.get("severity"),
                            "state": x.get("stateLabel") or x.get("state"),
                            "asset": x.get("assetName"), "asset_id": x.get("assetId"),
                            "created": x.get("created")}) for x in f]
    out: dict[str, Any] = {"findings": rows, "count": len(rows), "page": page}
    if len(rows) == page_size:
        out["next_page"] = page + 1
    return out


@tool(RO, "List assets")
async def list_assets(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    search: Annotated[Optional[str], Field(description="Text search.")] = None,
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """Assets discovered by or linked to a workspace."""
    api = SESSION.api(ctx)
    a = (await api.graphql(Q.ASSETS, {"ws": _ws(ctx, workspace_id), "page": page,
                                      "size": page_size, "search": search},
                           op="workspaceAssets"))["workspaceAssets"] or []
    rows = [render.compact({"id": x.get("id"), "name": x.get("name"),
                            "type": x.get("typeLabel") or x.get("type"),
                            "target": x.get("target"),
                            "sensitivity": x.get("sensitivityLabel") or x.get("sensitivity"),
                            "created": x.get("created")}) for x in a]
    out: dict[str, Any] = {"assets": rows, "count": len(rows), "page": page}
    if len(rows) == page_size:
        out["next_page"] = page + 1
    return out


@tool(RO, "List workspace files")
async def list_files(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    path: Annotated[str, Field(description="Folder to list, e.g. 'reports/'. Empty = root.")] = "",
    recursive: Annotated[bool, Field(description="List the whole tree under path.")] = False,
    search: Annotated[Optional[str], Field(description="Filter by file name.")] = None,
) -> dict[str, Any]:
    """Browse files and folders agents wrote in a workspace (reports, evidence, data)."""
    api = SESSION.api(ctx)
    f = (await api.graphql(Q.FILES, {"ws": _ws(ctx, workspace_id), "path": path,
                                     "recursive": recursive, "search": search or ""},
                           op="workspaceFiles"))["workspaceFiles"] or []
    rows = [render.compact({"path": x.get("path") + "/", "folder": True}
                           if x.get("isFolder") else
                           {"path": x.get("path"), "size": x.get("size"),
                            "modified": x.get("lastModified"), "type": x.get("contentType")})
            for x in f]
    return {"files": rows, "count": len(rows)}


def _decode_text(data: bytes, *, truncated: bool) -> str | None:
    """UTF-8 text, or None if it's binary. A cut at max_bytes may split a
    multi-byte character, so a truncated tail is allowed to lose up to 3 bytes."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        if truncated and e.start >= len(data) - 3:
            return data[: e.start].decode("utf-8", errors="strict")
        return None


SECRET_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "credentials", "config.json",
                "known_hosts", "authorized_keys", ".netrc", ".pgpass", ".npmrc", ".pypirc"}


def _safe_local_file(local_path: str) -> Path:
    """Resolve a path the model asked to upload, refusing obvious secrets.

    Agent output comes from systems under test and is untrusted, so a prompt
    injection could ask for ``~/.ssh/id_rsa`` or this server's own key store.
    """
    from .config import config_dir

    p = Path(local_path).expanduser().resolve()
    if not p.is_file():
        raise ToolError(f"No such file: {p}")
    roots = [Path(r).expanduser().resolve() for r in
             os.getenv("STROBES_MCP_UPLOAD_ROOTS", "").split(os.pathsep) if r.strip()]
    roots = roots or [Path.cwd().resolve()]
    if not any(p.is_relative_to(r) for r in roots) or roots == [Path(p.anchor)]:
        raise ToolError(f"Refusing to upload {p}: local uploads are limited to "
                        f"{', '.join(map(str, roots))} (the project the server runs in). Set "
                        "STROBES_MCP_UPLOAD_ROOTS to allow other folders, or pass `content`.")
    secrets_dir = config_dir().expanduser().resolve()
    hidden = [part for part in p.parts[1:] if part.startswith(".")]
    if (p.is_relative_to(secrets_dir) or hidden or p.name.lower() in SECRET_NAMES
            or p.name.startswith(".env") or p.suffix.lower() in (".pem", ".key", ".p12", ".pfx")):
        raise ToolError(f"Refusing to upload {p}: it looks like a credential or a hidden "
                        "file. Pass the content explicitly with `content` if the user "
                        "really wants it uploaded.")
    return p


TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml",
              "application/yaml", "application/javascript", "application/csv")


@tool(RO, "Read a workspace file")
async def read_file(
    ctx: Context,
    path: Annotated[str, Field(description="File path inside the workspace (from list_files).")],
    workspace_id: WorkspaceId = None,
    max_bytes: Annotated[int, Field(ge=1000, le=2_000_000, description=(
        "Inline at most this many bytes of text."))] = 200_000,
) -> dict[str, Any]:
    """Read a workspace file. Text files are returned inline (truncated at
    max_bytes); binary files return a short-lived download URL."""
    api = SESSION.api(ctx)
    ws = _ws(ctx, workspace_id)
    url = (await api.graphql(Q.FILE_URL, {"ws": ws, "path": path},
                             op="workspaceFileDownloadUrl"))["workspaceFileDownloadUrl"]
    if not url:
        raise ToolError(f"File not found: {path}")
    out: dict[str, Any] = {"path": path}
    async with httpx.AsyncClient(timeout=60, verify=api.profile.verify_tls,
                                 follow_redirects=True) as http:
        async with http.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            size = int(r.headers.get("content-length") or 0) or None
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    break
    data = bytes(buf[:max_bytes])
    is_text = ctype.startswith(TEXT_TYPES) or Path(path).suffix.lower() in (
        ".md", ".txt", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".py",
        ".js", ".ts", ".sh", ".har", ".sarif", ".jsonl", ".toml", ".ini")
    text = _decode_text(data, truncated=len(buf) > max_bytes) \
        if is_text or b"\x00" not in data[:1024] else None
    if text is None:
        out.update(binary=True, content_type=ctype, size=size, download_url=url,
                   note="Binary file — fetch download_url (expires soon).")
        return render.compact(out)
    out.update(content_type=ctype or None, size=size, content=text,
               truncated=len(buf) > max_bytes or None)
    return render.compact(out)


@tool(RO, "Get a download link")
async def get_download_url(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    path: Annotated[str, Field(description=(
        "A file path for that file, a folder path for a zip of it, or empty for a zip "
        "of the whole workspace."))] = "",
    as_zip: Annotated[bool, Field(description="Zip the path (folder) instead of one file.")] = False,
) -> dict[str, Any]:
    """Short-lived download URL for a workspace file, folder (zip) or the whole
    workspace (zip). Hand it to the user or fetch it with a shell tool."""
    api = SESSION.api(ctx)
    ws = _ws(ctx, workspace_id)
    if path and not as_zip:
        url = (await api.graphql(Q.FILE_URL, {"ws": ws, "path": path}))["workspaceFileDownloadUrl"]
        kind = "file"
    else:
        url = (await api.graphql(Q.ZIP_URL, {"ws": ws, "path": path}, op="workspaceDownloadUrl",
                                timeout=180))["workspaceDownloadUrl"]
        kind = "zip"
    if not url:
        raise ToolError("Nothing to download at that path.")
    return {"download_url": url, "kind": kind, "path": path or "(entire workspace)"}


@tool(WRITE_IDEMPOTENT, "Upload a file to a workspace")
async def upload_file(
    ctx: Context,
    path: Annotated[str, Field(description="Destination path inside the workspace, e.g. "
                                           "'docs/scope.md'. Agents read files from here.")],
    content: Annotated[Optional[str], Field(description="Text content to upload.")] = None,
    local_path: Annotated[Optional[str], Field(description=(
        "Upload this local file instead of `content` (local/stdio servers only)."))] = None,
    workspace_id: WorkspaceId = None,
) -> dict[str, Any]:
    """Put a file into a workspace (scope docs, API specs, credentials notes, prior
    reports …) so agents can use it. Overwrites a file at the same path."""
    if (content is None) == (local_path is None):
        raise ToolError("Pass exactly one of `content` or `local_path`.")
    if local_path is not None:
        _local_only(ctx, "Uploading a local file")
        p = _safe_local_file(local_path)
        body = p.read_bytes()
    else:
        body = content.encode("utf-8")
    if not body:
        raise ToolError("Refusing to upload an empty file.")
    api = SESSION.api(ctx)
    ws = _ws(ctx, workspace_id)
    res = await api.rest("POST", f"cli/workspaces/{ws}/upload/",
                         params={"path": path.lstrip("/")}, content=body,
                         headers={"Content-Type": "application/octet-stream"})
    return {"uploaded": True, "workspace_id": ws, **(res or {})}


@tool(RO, "List approval requests")
async def list_approvals(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    state: Annotated[Optional[Literal["pending", "approved", "rejected", "expired"]],
                     Field(description="Filter by state.")] = "pending",
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Actions agents asked a human to approve in this workspace (e.g. risky
    tests). Approve or reject them in the Strobes web app."""
    api = SESSION.api(ctx)
    ws = _ws(ctx, workspace_id)
    rows = await api.rest("GET", f"cli/workspaces/{ws}/approvals/",
                          params={"state": state, "limit": limit}) or []
    return {"approvals": [render.compact(r) for r in rows], "count": len(rows),
            "web_url": api.profile.app_url(f"ai/workspaces/{ws}")}


@tool(RO, "AI credit usage")
async def get_credit_usage(
    ctx: Context,
    workspace_id: Annotated[Optional[str], Field(description=(
        "Totals for one workspace. Omit for organization totals with a per-workspace "
        "breakdown."))] = None,
) -> dict[str, Any]:
    """AI credits and tokens consumed by agent runs."""
    api = SESSION.api(ctx)
    data = await api.rest("GET", "cli/credits/", params={"workspace_id": workspace_id}) or {}
    if isinstance(data.get("by_workspace"), list):
        data["by_workspace"] = data["by_workspace"][:25]
    return data


# --------------------------------------------------------------------------
# workflows
# --------------------------------------------------------------------------
@tool(RO, "List workflow templates")
async def list_workflow_templates(ctx: Context) -> dict[str, Any]:
    """Predefined multi-phase agent pipelines `start_workflow` can launch, with the
    variables each one requires."""
    api = SESSION.api(ctx)
    t = (await api.graphql(Q.TEMPLATES, op="workflowTemplates"))["workflowTemplates"] or []
    rows = [render.compact({"slug": x.get("slug"), "name": x.get("name"),
                            "description": render.truncate(x.get("description") or "", 300),
                            "phases": x.get("phaseCount"),
                            "required_variables": render.loads(x.get("requiredVariables"))})
            for x in t]
    return {"templates": rows, "count": len(rows)}


@tool(WRITE, "Start a workflow")
async def start_workflow(
    ctx: Context,
    template_slug: Annotated[str, Field(description="Template slug from list_workflow_templates.")],
    variables: Annotated[dict[str, Any], Field(description=(
        "The template's required variables, e.g. {\"target\": \"https://app.example.com\"}."))],
    name: Annotated[Optional[str], Field(description="Workspace name (defaults to the template name).")] = None,
    engagement_id: Annotated[Optional[str], Field(description="Link to an existing engagement.")] = None,
    as_engagement: Annotated[bool, Field(description=(
        "Also create a self-managed agentic engagement for it (shows up as an AI Pentest)."))] = False,
    start_date: Annotated[Optional[str], Field(description=(
        "YYYY-MM-DD to schedule the start (with as_engagement). Default: now."))] = None,
    activate: Annotated[bool, Field(description=(
        "Skip the interactive setup chat so the workflow starts now. Set false to "
        "finish setup in the web app first."))] = True,
) -> dict[str, Any]:
    """Launch a multi-phase agent workflow: creates a workspace from a template and
    starts its pipeline. Consumes AI credits. Follow it with `get_workflow`."""
    api = SESSION.api(ctx)
    p = (await api.graphql(Q.CREATE_FROM_TEMPLATE, {
        "org": api.profile.org_id, "slug": template_slug, "vars": variables, "name": name,
        "engagement": engagement_id, "engagementMode": as_engagement or None,
        "startDate": start_date}, op="createWorkspaceFromTemplate"))["createWorkspaceFromTemplate"] or {}
    w = render.workspace(p.get("workspace"))
    if activate and w.get("id") and w.get("status") != "active":
        w["status"] = await _activate(api, w["id"])
    w["web_url"] = api.profile.app_url(f"ai/workspaces/{w.get('id')}")
    w["next_step"] = ("Workflow starting — call get_workflow for phase progress." if activate
                      else "Finish workspace setup in the web app (web_url) to start the workflow.")
    return w


@tool(RO, "Get workflow progress")
async def get_workflow(
    ctx: Context,
    workspace_id: WorkspaceId = None,
    workflow_id: Annotated[Optional[str], Field(description="A specific workflow (default: latest).")] = None,
) -> dict[str, Any]:
    """Status of a workspace's workflow: overall state, current phase, every phase's
    status and task progress."""
    api = SESSION.api(ctx)
    wf = (await api.graphql(Q.WORKFLOW, {"ws": _ws(ctx, workspace_id), "wf": workflow_id},
                            op="workspaceWorkflow"))["workspaceWorkflow"]
    if not wf:
        return {"workflow": None, "note": "This workspace has no workflow."}
    out = render.compact(render.snake_keys(wf))
    for k in ("status", "current_phase_key"):
        out[k] = lower_or(out.get(k)) if k == "status" else out.get(k)
    for ph in out.get("phases") or []:
        ph["status"] = lower_or(ph.get("status"))
    return out


@tool(DESTRUCTIVE, "Pause / resume / cancel a workflow")
async def control_workflow(
    ctx: Context,
    action: Annotated[Literal["pause", "resume", "cancel"], Field(description=(
        "pause: hold after running tasks; resume: continue; cancel: stop for good."))],
    workspace_id: WorkspaceId = None,
    workflow_id: Annotated[Optional[str], Field(description="A specific workflow (default: latest).")] = None,
) -> dict[str, Any]:
    """Pause, resume or cancel a workspace's workflow."""
    api = SESSION.api(ctx)
    doc, field = {"pause": (Q.PAUSE_WORKFLOW, "pauseWorkflow"),
                  "resume": (Q.RESUME_WORKFLOW, "resumeWorkflow"),
                  "cancel": (Q.CANCEL_WORKFLOW, "cancelWorkflow")}[action]
    p = (await api.graphql(doc, {"ws": _ws(ctx, workspace_id), "wf": workflow_id},
                           op=field))[field] or {}
    return {"ok": bool(p.get("success")), "action": action}


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------
@mcp.prompt(title="Run a Strobes agent on a target")
def run_agent(target: str, goal: str = "a focused web application pentest") -> str:
    """Spin up the right Strobes agent for a target and report back."""
    return (
        f"Use the Strobes tools to run {goal} against {target}.\n"
        "1. strobes_whoami (sign in with strobes_login if needed).\n"
        "2. list_agents and pick the best-suited agent; list_workspaces (status=active) "
        "to reuse a relevant workspace, else create_workspace.\n"
        "3. create_task with precise instructions: target, scope boundaries, what to "
        "deliver. Then wait_for_task until it finishes.\n"
        "4. Summarize the result, then list_findings and list_files for evidence/reports.\n"
        "Stay strictly within the stated target; ask before anything destructive.")


@mcp.prompt(title="Summarize a workspace")
def summarize_workspace(workspace_id: str) -> str:
    """Brief on everything agents did and found in a workspace."""
    return (
        f"Summarize Strobes workspace {workspace_id}: get_workspace for stats, list_tasks "
        "for what ran (and get_task on any failed ones), list_findings (critical/high "
        "first), list_files for reports. Produce an executive summary, key findings "
        "with severity, open/failed work, and suggested next agent tasks.")


def build(transport: str = "stdio") -> FastMCP:
    os.environ["STROBES_MCP_TRANSPORT"] = transport
    return mcp
