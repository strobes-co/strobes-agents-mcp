"""Tools exercised through a real in-memory MCP client session (mocked HTTP)."""

import json

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from strobes_agents_mcp import server
from conftest import GQL, ORG, REST, TASK, WS


@pytest.fixture(autouse=True)
def fresh_session():
    server.SESSION.__init__()
    yield


async def call(tool, **args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        res = await s.call_tool(tool, args)
        text = res.content[0].text if res.content else ""
        return res, (json.loads(text) if not res.isError else text)


def gql_router(handlers):
    """Dispatch mocked GraphQL by the root field in the query."""
    def handle(request):
        body = json.loads(request.content)
        for key, resp in handlers.items():
            if key in body["query"]:
                out = resp(body) if callable(resp) else resp
                return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)
        return httpx.Response(200, json={"errors": [{"message": "unmocked"}]})
    return handle


async def test_tool_catalog():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        tools = (await s.list_tools()).tools
        names = {t.name for t in tools}
        assert {"strobes_login", "create_task", "wait_for_task", "start_workflow"} <= names
        for t in tools:
            assert t.annotations is not None, t.name
            assert t.description and len(t.description) > 20, t.name
        ro = {t.name for t in tools if t.annotations.readOnlyHint}
        assert "list_findings" in ro and "create_task" not in ro
        assert next(t for t in tools if t.name == "cancel_task").annotations.destructiveHint


async def test_whoami_not_signed_in():
    res, out = await call("strobes_whoami")
    assert not res.isError
    assert out["signed_in"] is False and "strobes_login" in out["next_step"]


async def test_tools_require_login():
    res, text = await call("list_workspaces")
    assert res.isError and "strobes_login" in text


@respx.mock
async def test_login_tool_flow(isolated):
    respx.post("https://app.strobes.co/api/v1/cli/auth/device/").respond(201, json={
        "device_code": "d", "user_code": "WXYZ-0000", "verification_path": "/cli/login?code=WXYZ-0000",
        "expires_in": 600, "interval": 1})
    respx.post("https://app.strobes.co/api/v1/cli/auth/device/token/").respond(200, json={
        "organization_id": ORG, "master_key": "mk", "api_base_url": "https://acme.strobes.co/api",
        "email": "me@acme.test"})
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        r = await s.call_tool("strobes_login", {"open_browser": False})
        out = json.loads(r.content[0].text)
        assert out["user_code"] == "WXYZ-0000" and out["verification_url"].endswith("WXYZ-0000")
        r = await s.call_tool("strobes_login_status", {"wait_seconds": 10})
        out = json.loads(r.content[0].text)
        assert out["status"] == "approved" and out["email"] == "me@acme.test"


@respx.mock
async def test_list_workspaces_and_search(signed_in):
    respx.post(GQL).mock(side_effect=gql_router({"workspaces(": {"data": {"workspaces": [
        {"id": "a", "name": "Acme Web", "status": "ACTIVE", "description": "long"},
        {"id": "b", "name": "Other", "status": "ARCHIVED"}]}}}))
    _, out = await call("list_workspaces", search="acme")
    assert [w["id"] for w in out["workspaces"]] == ["a"]
    assert out["workspaces"][0]["status"] == "active"
    sent = json.loads(respx.calls[0].request.content)
    assert sent["variables"]["org"] == ORG
    assert respx.calls[0].request.headers["authorization"] == "token " + "k" * 40


@respx.mock
async def test_create_task_defaults_and_wait(signed_in, monkeypatch):
    statuses = iter(["QUEUED", "RUNNING", "COMPLETED"])

    def tasks(body):
        return {"data": {"workspaceTasks": [{"id": TASK, "title": "t", "status": next(statuses, "COMPLETED"),
                                             "outputData": json.dumps({"thread_id": "th"})}]}}

    respx.post(GQL).mock(side_effect=gql_router({
        "createWorkspaceTask(": {"data": {"createWorkspaceTask": {"task": {"id": TASK, "status": "QUEUED"}}}},
        "workspaceTaskMessages(": {"data": {"workspaceTaskMessages": [
            {"id": "m1", "author": "ORCHESTRATOR", "createdAt": "2026-01-01T00:00:01",
             "blocks": [{"type": "thinking", "thinking": "secret reasoning"},
                        {"type": "text", "text": "All done: 2 issues."}]}]}},
        "workspaceTasks(": tasks,
    }))
    monkeypatch.setattr(server, "_sleep", _no_sleep)  # fast polling
    res, out = await call("create_task", instructions="scan it", agent="web_pentest_agent",
                          wait_seconds=60)
    assert not res.isError, out
    assert out["status"] == "completed" and out["result"] == "All done: 2 issues."
    create = json.loads(respx.calls[0].request.content)
    assert create["variables"]["ws"] == WS  # default workspace from sign-in
    assert create["variables"]["taskType"] == "custom"
    assert "createWorkspaceTask" in create["query"]  # MasterKey allowlist needs the name


async def _no_sleep(*_a, **_k):
    return None


@respx.mock
async def test_messages_render_drops_thinking(signed_in):
    respx.post(GQL).mock(side_effect=gql_router({"workspaceTaskMessages(": {"data": {"workspaceTaskMessages": [
        {"id": "2", "author": "ORCHESTRATOR", "createdAt": "2", "blocks": json.dumps([
            {"type": "thinking", "thinking": "x" * 5000},
            {"type": "tool_use", "name": "execute_command", "input": {"command": "nmap -sV host\nmore"}},
            {"type": "text", "text": "Found port 443."}])},
        {"id": "1", "author": "USER", "createdAt": "1", "blocks": [{"type": "text", "text": "go"}]}]}}}))
    _, out = await call("get_task_messages", task_id=TASK, limit=2)
    assert [m["id"] for m in out["messages"]] == ["1", "2"]  # oldest → newest
    text = out["messages"][1]["text"]
    assert "xxxx" not in text and "[tool execute_command command=nmap -sV host]" in text
    assert out["older_offset"] == 2


@respx.mock
async def test_activate_falls_back_to_rest(signed_in):
    respx.post(GQL).mock(side_effect=gql_router({
        "skipWorkspaceSetup(": httpx.Response(400, json={"errors": [
            {"message": "Cannot query field 'skipWorkspaceSetup' on type 'PublicMutation'."}]}),
        "workspace(id:": {"data": {"workspace": {"id": WS, "status": "ACTIVE"}}},
    }))
    rest = respx.post(f"{REST}/cli/threads/create/").respond(201, json={"id": "t"})
    res, out = await call("activate_workspace")
    assert not res.isError, out
    assert out["status"] == "active" and rest.called


@respx.mock
async def test_auth_error_is_actionable(signed_in):
    respx.post(GQL).respond(401, json={"detail": "bad"})
    res, text = await call("list_tasks")
    assert res.isError and "strobes_login" in text and "401" in text


@respx.mock
async def test_mutation_not_retried_but_query_is(signed_in):
    route = respx.post(GQL)
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={"data": {"workspaces": []}})]
    _, out = await call("list_workspaces")
    assert out["count"] == 0 and route.call_count == 2

    route.side_effect = [httpx.Response(503)]
    route.reset()
    res, _ = await call("cancel_task", task_id=TASK)
    assert res.isError and route.call_count == 1


@respx.mock
async def test_header_auth_in_http_mode(signed_in, monkeypatch):
    """Remote clients can supply their own key per request."""
    from strobes_agents_mcp.config import Profile

    class Req:
        headers = {"authorization": "Bearer remote-key", "x-strobes-organization-id": "org2",
                   "x-strobes-url": "https://beta.strobes.co"}

    class Ctx:
        class request_context:
            request = Req()

    p = server._request_profile(Ctx())
    assert isinstance(p, Profile) and p.master_key == "remote-key" and p.org_id == "org2"
    assert p.graphql_url == "https://beta.strobes.co/api/public/graphql/" and p.source == "request"
    monkeypatch.setenv("STROBES_MCP_REQUIRE_HEADER_AUTH", "1")
    with pytest.raises(Exception, match="per-request credentials"):
        server.SESSION.profile(None)


@respx.mock
async def test_upload_file(signed_in):
    up = respx.post(f"{REST}/cli/workspaces/{WS}/upload/").respond(
        201, json={"path": "docs/scope.md", "size": 5, "content_type": "text/markdown"})
    _, out = await call("upload_file", path="/docs/scope.md", content="hello")
    assert out["uploaded"] and up.calls[0].request.content == b"hello"
    assert up.calls[0].request.url.params["path"] == "docs/scope.md"


@respx.mock
async def test_findings_severity_filter(signed_in):
    respx.post(GQL).mock(side_effect=gql_router({"workspaceFindings(": {"data": {"workspaceFindings": [
        {"id": 1, "title": "XSS", "severityLabel": "High", "stateLabel": "New"}]}}}))
    _, out = await call("list_findings", severity=["critical", "high"])
    assert out["findings"][0]["severity"] == "high"
    assert json.loads(respx.calls[0].request.content)["variables"]["severity"] == [5, 4]



class _Req:
    def __init__(self, **headers):
        self.headers = headers


class _Ctx:
    def __init__(self, req):
        class RC:
            request = req
        self.request_context = RC()


def test_remote_url_allowlist(signed_in, monkeypatch):
    ok = server._request_profile(_Ctx(_Req(authorization="Bearer k",
                                            **{"x-strobes-url": "https://beta.strobes.co"})))
    assert ok.web_url == "https://beta.strobes.co" and ok.org_id == ""  # nothing inherited
    for bad in ("http://169.254.169.254", "https://evil.example", "http://beta.strobes.co"):
        with pytest.raises(Exception, match="allowed Strobes URL"):
            server._request_profile(_Ctx(_Req(authorization="Bearer k", **{"x-strobes-url": bad})))
    monkeypatch.setenv("STROBES_MCP_ALLOWED_URLS", "*.corp.internal")
    assert server._request_profile(_Ctx(_Req(authorization="Bearer k",
                                             **{"x-strobes-url": "https://s.corp.internal"})))


def test_remote_key_without_url_uses_operator_url_but_not_workspace(signed_in):
    p = server._request_profile(_Ctx(_Req(authorization="Bearer k")))
    assert p.web_url == "https://acme.strobes.co" and p.org_id == ORG
    assert p.workspace_id is None and p.master_key == "k"


@pytest.mark.parametrize("tool", ["strobes_login", "strobes_logout", "strobes_use_profile",
                                  "strobes_login_status"])
async def test_session_tools_refuse_remote_clients(signed_in, monkeypatch, tool):
    monkeypatch.setenv("STROBES_MCP_TRANSPORT", "streamable-http")
    args = {"profile": "default"} if tool == "strobes_use_profile" else {}
    res, text = await call(tool, **args)
    assert res.isError and "only available when the server runs locally" in text
    assert server.resolve_profile().complete  # host login untouched


async def test_upload_refuses_secrets(signed_in, tmp_path):
    from strobes_agents_mcp.config import config_path
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE")
    for path in (str(config_path()), str(secret)):
        res, text = await call("upload_file", path="x", local_path=path)
        assert res.isError and ("Refusing" in text or "only available" in text), text


def test_decode_text_tolerates_split_multibyte():
    data = ("a" * 999 + "é").encode()[:1000]
    assert server._decode_text(data, truncated=True) == "a" * 999
    assert server._decode_text(b"\xff\xfe\x00binary", truncated=False) is None


@respx.mock
async def test_login_switches_session_to_new_profile(signed_in):
    respx.post("https://app.strobes.co/api/v1/cli/auth/device/").respond(201, json={
        "device_code": "d", "user_code": "C", "expires_in": 600, "interval": 1})
    respx.post("https://app.strobes.co/api/v1/cli/auth/device/token/").respond(200, json={
        "organization_id": "org-eu", "master_key": "eu-key",
        "api_base_url": "https://eu.strobes.co/api"})
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        await s.call_tool("strobes_login", {"profile": "eu", "url": "https://app.strobes.co",
                                            "open_browser": False})
        r = await s.call_tool("strobes_login_status", {"wait_seconds": 10})
        assert json.loads(r.content[0].text)["status"] == "approved"
    p = server.SESSION.profile(None)
    assert p.name == "eu" and p.master_key == "eu-key"  # not the old default


@respx.mock
async def test_rest_redirect_is_an_error(signed_in):
    respx.post(f"{REST}/cli/workspaces/{WS}/upload/").respond(
        301, headers={"location": "https://elsewhere/"})
    res, text = await call("upload_file", path="a.md", content="x")
    assert res.isError and "redirect" in text


@respx.mock
async def test_concurrent_calls_share_clients_safely(signed_in, monkeypatch):
    """A finished call must not close the client another call is still using."""
    import asyncio
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return httpx.Response(200, json={"data": {"workspaces": []}})

    respx.post(GQL).mock(side_effect=slow)
    slow_call = asyncio.create_task(call("list_workspaces"))
    await asyncio.sleep(0.05)
    respx.post(GQL).mock(return_value=httpx.Response(200, json={"data": {"workspaces": []}}))
    res, _ = await call("list_workspaces")  # opens & ends its own MCP session
    assert not res.isError
    gate.set()
    res, out = await slow_call
    assert not res.isError, out



async def test_upload_limited_to_project_roots(signed_in, tmp_path, monkeypatch):
    proj, other = tmp_path / "proj", tmp_path / "elsewhere"
    proj.mkdir(); other.mkdir()
    (proj / "scope.md").write_text("in scope")
    (other / "claude_desktop_config.json").write_text("{}")
    monkeypatch.chdir(proj)
    res, text = await call("upload_file", path="x", local_path=str(other / "claude_desktop_config.json"))
    assert res.isError and "limited to" in text
    monkeypatch.setenv("STROBES_MCP_UPLOAD_ROOTS", str(other))
    res, text = await call("upload_file", path="x", local_path=str(proj / "scope.md"))
    assert res.isError and "limited to" in text


def test_require_header_auth_never_exposes_stored_login(signed_in, monkeypatch):
    monkeypatch.setenv("STROBES_MCP_REQUIRE_HEADER_AUTH", "1")
    p = server._request_profile(_Ctx(_Req(authorization="Bearer anything")))
    assert p.base_url == "" and p.org_id == "" and not p.complete
    monkeypatch.setenv("STROBES_URL", "https://acme.strobes.co")
    monkeypatch.setenv("STROBES_ORGANIZATION_ID", "deploy-org")
    p = server._request_profile(_Ctx(_Req(authorization="Bearer anything")))
    assert p.org_id == "deploy-org"  # operator's deployment env is fine


async def test_loopback_http_allows_session_tools(signed_in, monkeypatch):
    monkeypatch.setenv("STROBES_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("STROBES_MCP_LOCAL_HTTP", "1")
    res, out = await call("strobes_use_profile", profile="default")
    assert not res.isError, out


def _wf(status="running", phase="recon", tasks=None, total=2, done=0, gate="auto"):
    return {"data": {"workspaceWorkflow": {
        "workflowId": "44444444-4444-4444-4444-444444444444", "status": status,
        "currentPhaseKey": phase, "totalTasks": total, "completedTasks": done,
        "phases": [{"phaseKey": phase, "phaseName": phase.title(), "order": 1,
                    "status": "RUNNING" if status == "running" else status.upper(),
                    "gateType": gate, "tasks": tasks or []}]}}}


async def test_prompts_catalog():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        names = {p.name for p in (await s.list_prompts()).prompts}
    assert {"run_agent", "summarize_workspace", "run_workflow", "monitor_workflow",
            "agent_status", "triage_findings", "investigate_task"} <= names


async def test_get_workflow_flags_input_request(signed_in):
    tasks = [{"taskId": TASK, "title": "Log in", "status": "RUNNING",
              "awaitingInput": {"interruptType": "otp", "message": "Enter the OTP"}}]
    with respx.mock:
        respx.post(GQL).mock(side_effect=gql_router({"workspaceWorkflow": _wf(tasks=tasks)}))
        res, out = await call("get_workflow")
    assert not res.isError
    assert out["needs_attention"][0]["kind"] == "input_request"
    assert out["phases"][0]["tasks"][0]["status"] == "running"
    assert out["progress"] == "0/2 tasks"


async def test_wait_for_workflow_returns_on_phase_change(signed_in, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(server, "_sleep", no_sleep)
    snaps = iter([
        _wf(tasks=[{"taskId": TASK, "title": "Recon", "status": "RUNNING"}]),
        _wf(tasks=[{"taskId": TASK, "title": "Recon", "status": "COMPLETED"}], done=1),
        _wf(phase="exploit", done=1),
    ])
    with respx.mock:
        respx.post(GQL).mock(side_effect=gql_router({"workspaceWorkflow": lambda b: next(snaps)}))
        res, out = await call("wait_for_workflow", poll_seconds=5)
    assert not res.isError
    assert out["returned_because"] == "phase_change"
    assert any(e.get("status") == "running → completed" for e in out["events"])
    assert any(e.get("phase") == "recon → exploit" for e in out["events"])


async def test_control_workflow_restart_phase_needs_key(signed_in):
    res, text = await call("control_workflow", action="restart_phase")
    assert res.isError and "phase_key" in text
    seen = {}
    def ok(body):
        seen.update(body["variables"])
        return {"data": {"restartWorkflowFromPhase": {"success": True}}}
    with respx.mock:
        respx.post(GQL).mock(side_effect=gql_router({"restartWorkflowFromPhase": ok}))
        res, out = await call("control_workflow", action="restart_phase", phase_key="exploit",
                              cascade=True)
    assert out["ok"] and seen["scope"] == "cascade" and seen["phase"] == "exploit"
