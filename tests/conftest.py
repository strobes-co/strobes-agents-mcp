import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ENV_VARS = [
    "STROBES_HOST", "STROBES_BASE_URL", "STROBES_URL", "STROBES_AI_BASE_URL",
    "STROBES_API_TOKEN", "STROBES_API_KEY", "STROBES_MASTER_KEY", "STROBES_AI_MASTER_KEY",
    "STROBES_ORGANIZATION_ID", "STROBES_ORG_ID", "STROBES_AI_ORG_ID", "STROBES_PROFILE",
    "STROBES_AI_PROFILE", "STROBES_VERIFY_TLS", "STROBES_INSECURE", "STROBES_TENANT_HOST",
    "STROBES_WORKSPACE_ID", "STROBES_AI_WORKSPACE_ID", "STROBES_AI_DEPLOYMENT",
    "STROBES_MCP_REQUIRE_HEADER_AUTH",
]

ORG = "11111111-1111-1111-1111-111111111111"
WS = "22222222-2222-2222-2222-222222222222"
TASK = "33333333-3333-3333-3333-333333333333"
GQL = "https://acme.strobes.co/api/public/graphql/"
REST = f"https://acme.strobes.co/api/v1/organizations/{ORG}"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets an empty config dir and a clean environment."""
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("STROBES_AI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STROBES_NO_BROWSER", "1")
    yield tmp_path / "home"


@pytest.fixture
def signed_in(isolated):
    isolated.mkdir(parents=True, exist_ok=True)
    (isolated / "config.json").write_text(json.dumps({
        "current_profile": "default",
        "profiles": {"default": {
            "base_url": "https://acme.strobes.co/api", "deployment": "direct",
            "org_id": ORG, "master_key": "k" * 40, "workspace_id": WS,
            "email": "me@acme.test", "thread_id": "keep-me"}},
        "workspace_dirs": {"x": "/tmp/x"},
    }))
    return isolated
