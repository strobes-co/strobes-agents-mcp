import json
import os
import stat

import httpx
import pytest
import respx

from strobes_agents_mcp import auth, config
from conftest import ORG, WS


def test_no_credentials():
    p = config.resolve_profile()
    assert not p.complete and p.source == "none"


def test_reads_cli_profile(signed_in):
    p = config.resolve_profile()
    assert p.complete and p.source == "config"
    assert p.graphql_url == "https://acme.strobes.co/api/public/graphql/"
    assert p.rest_url == "https://acme.strobes.co/api/v1"
    assert p.web_url == "https://acme.strobes.co"
    assert p.workspace_id == WS


@pytest.mark.parametrize("base,dep,gql", [
    ("https://acme.strobes.co", "", "https://acme.strobes.co/api/public/graphql/"),
    ("acme.strobes.co", "", "https://acme.strobes.co/api/public/graphql/"),
    ("https://acme.strobes.co/api/", "direct", "https://acme.strobes.co/api/public/graphql/"),
    ("http://localhost:8000", "direct", "http://localhost:8000/public/graphql/"),
])
def test_url_derivation(base, dep, gql):
    assert config.Profile(base_url=base, deployment=dep).graphql_url == gql


def test_env_credentials_replace_profile_wholesale(signed_in, monkeypatch):
    monkeypatch.setenv("STROBES_API_KEY", "envkey")
    monkeypatch.setenv("STROBES_URL", "https://beta.strobes.co")
    monkeypatch.setenv("STROBES_ORGANIZATION_ID", "org-b")
    p = config.resolve_profile()
    assert (p.master_key, p.org_id, p.web_url, p.source) == (
        "envkey", "org-b", "https://beta.strobes.co", "env")
    assert p.email is None  # the stored profile's identity doesn't leak in


def test_env_key_never_paired_with_stored_url(signed_in, monkeypatch):
    """A key from the environment must not be sent to the stored profile's server."""
    monkeypatch.setenv("STROBES_API_KEY", "envkey")
    p = config.resolve_profile()
    assert not p.complete and p.source == "env-incomplete"


def test_env_key_fills_from_keyless_profile(isolated, monkeypatch):
    isolated.mkdir(parents=True)
    (isolated / "config.json").write_text(json.dumps({"profiles": {"default": {
        "base_url": "https://acme.strobes.co", "org_id": ORG}}}))
    monkeypatch.setenv("STROBES_API_KEY", "envkey")
    p = config.resolve_profile()
    assert p.complete and p.org_id == ORG and p.source == "env"


def test_explicit_profile_ignores_env_credentials(signed_in, monkeypatch):
    monkeypatch.setenv("STROBES_API_KEY", "envkey")
    monkeypatch.setenv("STROBES_URL", "https://beta.strobes.co")
    monkeypatch.setenv("STROBES_ORGANIZATION_ID", "org-b")
    p = config.resolve_profile("default")
    assert p.master_key == "k" * 40 and p.org_id == ORG and p.source == "config"


@pytest.mark.parametrize("val,verify", [("0", True), ("false", True), ("1", False), ("yes", False)])
def test_insecure_is_a_real_boolean(signed_in, monkeypatch, val, verify):
    monkeypatch.setenv("STROBES_INSECURE", val)
    assert config.resolve_profile().verify_tls is verify


def test_corrupt_config_is_never_overwritten(isolated):
    isolated.mkdir(parents=True)
    bad = '{"profiles": {"work": {"master_key": "keep"}},}'
    (isolated / "config.json").write_text(bad)
    with pytest.raises(config.ConfigError, match="isn't valid JSON"):
        config.save_login("default", base_url="https://a.strobes.co/api", org_id=ORG,
                          master_key="new", workspace_id=None, email=None, login_url="x")
    assert (isolated / "config.json").read_text() == bad
    assert config.resolve_profile().source == "none"  # reads stay forgiving


def test_legacy_env_only(monkeypatch):
    monkeypatch.setenv("STROBES_HOST", "acme.strobes.co")
    monkeypatch.setenv("STROBES_API_TOKEN", "t")
    monkeypatch.setenv("STROBES_ORGANIZATION_ID", ORG)
    monkeypatch.setenv("STROBES_VERIFY_TLS", "false")
    p = config.resolve_profile()
    assert p.complete and p.source == "env" and not p.verify_tls
    assert p.graphql_url == "https://acme.strobes.co/api/public/graphql/"
    assert p.tenant_host is None


def test_legacy_base_url_plus_host_sets_host_header(monkeypatch):
    monkeypatch.setenv("STROBES_API_TOKEN", "t")
    monkeypatch.setenv("STROBES_ORGANIZATION_ID", ORG)
    monkeypatch.setenv("STROBES_HOST", "acme.strobes.co")
    monkeypatch.setenv("STROBES_BASE_URL", "http://localhost:8000")
    p = config.resolve_profile()
    assert p.tenant_host == "acme.strobes.co"
    assert p.graphql_url == "http://localhost:8000/api/public/graphql/"


def test_profile_selection(signed_in, monkeypatch):
    raw = config.load_raw()
    raw["profiles"]["eu"] = {"base_url": "https://eu.strobes.co", "org_id": "o", "master_key": "m"}
    config.save_raw(raw)
    monkeypatch.setenv("STROBES_PROFILE", "eu")
    assert config.resolve_profile().org_id == "o"
    assert [p["profile"] for p in config.list_profiles()] == ["default", "eu"]


def test_save_login_preserves_cli_keys_and_is_private(signed_in):
    config.save_login("default", base_url="https://acme.strobes.co/api", org_id=ORG,
                      master_key="new", workspace_id=None, email="me@acme.test",
                      login_url="https://acme.strobes.co")
    raw = json.loads((signed_in / "config.json").read_text())
    assert raw["workspace_dirs"] == {"x": "/tmp/x"}
    assert raw["profiles"]["default"]["master_key"] == "new"
    assert raw["profiles"]["default"]["thread_id"] == "keep-me"  # same org
    if os.name != "nt":
        assert stat.S_IMODE((signed_in / "config.json").stat().st_mode) == 0o600


def test_logout_keeps_url(signed_in):
    assert config.clear_login("default")
    p = config.resolve_profile()
    assert not p.complete and p.base_url
    assert not config.clear_login("default")


# ------------------------------------------------------------- device flow
START = "https://app.strobes.co/api/v1/cli/auth/device/"
TOKEN = "https://app.strobes.co/api/v1/cli/auth/device/token/"


@respx.mock
async def test_device_flow_approved(isolated):
    respx.post(START).respond(201, json={
        "device_code": "secret", "user_code": "ABCD-1234",
        "verification_path": "/cli/login?code=ABCD-1234", "expires_in": 600, "interval": 1})
    token = respx.post(TOKEN)
    token.side_effect = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"organization_id": ORG, "master_key": "mk",
                                  "api_base_url": "https://acme.strobes.co/api",
                                  "workspace_id": WS, "workspace_name": "Acme",
                                  "email": "me@acme.test"}),
    ]
    login = await auth.start_login(None, launch_browser=False)
    assert login.verification_url == "https://app.strobes.co/cli/login?code=ABCD-1234"
    assert "Strobes MCP" in json.loads(respx.calls[0].request.content)["client_name"]
    login.interval = 0.01
    await auth.wait_for_login(login)
    assert login.status == "approved"
    assert json.loads(token.calls[0].request.content) == {"device_code": "secret"}
    p = config.resolve_profile()
    assert p.complete and p.master_key == "mk" and p.web_url == "https://acme.strobes.co"
    assert p.workspace_id == WS and p.email == "me@acme.test"
    assert "master_key" not in login.public() and "mk" not in json.dumps(login.public())


@respx.mock
async def test_device_flow_denied(isolated):
    respx.post(START).respond(201, json={"device_code": "d", "user_code": "U",
                                         "expires_in": 600, "interval": 1})
    respx.post(TOKEN).respond(400, json={"error": "access_denied"})
    login = await auth.start_login(None, launch_browser=False)
    await auth.wait_for_login(login)
    assert login.status == "denied"
    assert not config.resolve_profile().complete


@respx.mock
async def test_device_flow_unsupported_server(isolated):
    respx.post(START).respond(404)
    with pytest.raises(auth.LoginError, match="doesn't support browser sign-in"):
        await auth.start_login(None, launch_browser=False)



@respx.mock
async def test_device_flow_expiry_is_reported(isolated):
    import time
    respx.post(START).respond(201, json={"device_code": "d", "user_code": "U",
                                         "expires_in": 1, "interval": 1})
    respx.post(TOKEN).respond(400, json={"error": "authorization_pending"})
    login = await auth.start_login(None, launch_browser=False)
    login.interval, login.expires_at = 0.05, time.time() + 0.2
    await auth.wait_for_login(login)
    assert login.status == "expired"


@respx.mock
async def test_device_flow_slow_down_backs_off(isolated):
    respx.post(START).respond(201, json={"device_code": "d", "user_code": "U",
                                         "expires_in": 600, "interval": 1})
    respx.post(TOKEN).respond(400, json={"error": "slow_down"})
    login = await auth.start_login(None, launch_browser=False)
    async with httpx.AsyncClient() as http:
        assert await auth.poll_once(login, http) is False
    assert login.interval == 6
