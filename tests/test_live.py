"""Live end-to-end test against a real Strobes tenant (opt-in).

    STROBES_LIVE=1 uv run pytest tests/test_live.py -s            # read-only checks
    STROBES_LIVE=1 STROBES_LIVE_WRITE=1 uv run pytest tests/test_live.py -s
                                                                    # + spins up an agent task

Uses your normal sign-in (`strobes-agents-mcp login`) or STROBES_* env vars;
STROBES_PROFILE picks the profile. The write test creates a workspace, runs a
tiny task, uploads/reads a file, then archives the workspace.
"""

import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.skipif(not os.getenv("STROBES_LIVE"), reason="set STROBES_LIVE=1")

LIVE_ENV = {k: v for k, v in os.environ.items() if k.startswith("STROBES_") or k in
            ("HOME", "PATH", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME")}


@pytest.fixture(autouse=True)
def isolated():  # override: live tests use the real config
    yield


async def _session():
    params = StdioServerParameters(command=sys.executable, args=["-m", "strobes_agents_mcp"],
                                   env={**LIVE_ENV, "PYTHONPATH": os.path.join(
                                       os.path.dirname(__file__), "..", "src")})
    return stdio_client(params)


async def call(s, tool, **args):
    res = await s.call_tool(tool, args)
    text = res.content[0].text if res.content else ""
    assert not res.isError, f"{tool}: {text}"
    return json.loads(text)


async def test_live_read_only():
    async with await _session() as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        me = await call(s, "strobes_whoami")
        assert me["connected"], me
        assert (await call(s, "list_agents"))["count"] > 0
        assert (await call(s, "list_workflow_templates"))["count"] > 0
        ws = (await call(s, "list_workspaces", status="active", limit=5))["workspaces"]
        if not ws:
            pytest.skip("no active workspace to read")
        wid = ws[0]["id"]
        assert (await call(s, "get_workspace", workspace_id=wid))["id"] == wid
        tasks = await call(s, "list_tasks", workspace_id=wid)
        await call(s, "list_findings", workspace_id=wid, page_size=5)
        await call(s, "list_assets", workspace_id=wid, page_size=5)
        await call(s, "list_files", workspace_id=wid)
        await call(s, "get_workflow", workspace_id=wid)
        await call(s, "list_approvals", workspace_id=wid)
        if tasks["tasks"]:
            tid = tasks["tasks"][0]["id"]
            await call(s, "get_task", workspace_id=wid, task_id=tid)
            await call(s, "get_task_messages", workspace_id=wid, task_id=tid, limit=3)


@pytest.mark.skipif(not os.getenv("STROBES_LIVE_WRITE"), reason="set STROBES_LIVE_WRITE=1")
async def test_live_spin_up_task():
    async with await _session() as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        ws = await call(s, "create_workspace", name="strobes-agents-mcp live test")
        assert ws["status"] == "active", ws
        try:
            t = await call(s, "create_task", workspace_id=ws["id"], agent="general_security_assistant",
                           instructions="Reply with exactly: MCP live OK. Do not use any tools.",
                           wait_seconds=300)
            assert t["status"] == "completed", t
            assert "MCP live OK" in (t.get("result") or "")
            await call(s, "upload_file", workspace_id=ws["id"], path="docs/live.md", content="# live\n")
            f = await call(s, "read_file", workspace_id=ws["id"], path="docs/live.md")
            assert f["content"] == "# live\n"
        finally:
            await s.call_tool("archive_workspace", {"workspace_id": ws["id"]})
