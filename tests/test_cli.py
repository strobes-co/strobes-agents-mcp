import json
from pathlib import Path

import pytest

from strobes_agents_mcp import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli, "_app_support", lambda: tmp_path / "AppSupport")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


CMD = ["uvx", "--from", "git+https://example/x", "strobes-agents-mcp"]


def test_launch_command_override_and_uvx(monkeypatch):
    assert cli.launch_command("foo --bar") == ["foo", "--bar"]
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    assert cli.launch_command()[:2] == ["uvx", "--from"]
    monkeypatch.setenv("STROBES_MCP_PACKAGE", "strobes-agents-mcp")
    assert cli.launch_command() == ["uvx", "strobes-agents-mcp"]


def test_cursor_merges_existing_servers(home):
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    assert cli.install("cursor", CMD, {"STROBES_PROFILE": "eu"}, "user", False) == 0
    data = json.loads(path.read_text())
    assert data["keep"] == 1 and "other" in data["mcpServers"]
    entry = data["mcpServers"]["strobes-agents"]
    assert entry == {"command": "uvx", "args": CMD[1:], "env": {"STROBES_PROFILE": "eu"}}


def test_claude_desktop_creates_file(home):
    assert cli.install("claude-desktop", CMD, {}, "user", False) == 0
    data = json.loads((home / "AppSupport" / "Claude" / "claude_desktop_config.json").read_text())
    assert data["mcpServers"]["strobes-agents"]["command"] == "uvx"


def test_invalid_json_is_not_clobbered(home):
    path = home / ".gemini" / "settings.json"
    path.parent.mkdir()
    path.write_text("{ not json")
    with pytest.raises(SystemExit):
        cli.install("gemini", CMD, {}, "user", False)
    assert path.read_text() == "{ not json"


def test_codex_appends_toml_once(home):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "o5"\n')
    assert cli.install("codex", CMD, {}, "user", False) == 0
    text = path.read_text()
    assert text.startswith('model = "o5"\n') and "[mcp_servers.strobes-agents]" in text
    assert 'command = "uvx"' in text
    assert cli.install("codex", CMD, {}, "user", False) == 1  # no duplicate block


def test_vscode_project_uses_servers_key(home, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli.install("vscode", CMD, {}, "project", False) == 0
    data = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert data["servers"]["strobes-agents"]["type"] == "stdio"


def test_claude_code_dry_run(capsys):
    assert cli.install("claude-code", CMD, {"STROBES_PROFILE": "eu"}, "user", True) == 0
    out = capsys.readouterr().out
    # -e is variadic in `claude mcp add`, so the server name must precede it
    assert "claude mcp add --scope user strobes-agents -e STROBES_PROFILE=eu -- uvx" in out


def test_status_not_signed_in(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["status", "--json"])
    assert e.value.code == 1
    assert json.loads(capsys.readouterr().out)["signed_in"] is False


def test_codex_detects_quoted_duplicate(home):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('[mcp_servers."strobes-agents"]\ncommand = "x"\n')
    assert cli.install("codex", CMD, {}, "user", False) == 1
    assert path.read_text().count("strobes-agents") == 1


def test_json_with_wrong_shape_is_refused(home):
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": []}')
    with pytest.raises(SystemExit):
        cli.install("cursor", CMD, {}, "user", False)
    assert path.read_text() == '{"mcpServers": []}'


@pytest.mark.parametrize("exe,ephemeral", [
    ("/Users/a/.cache/uv/archive-v0/x/bin/strobes-agents-mcp", True),
    (r"C:\Users\a\AppData\Local\uv\cache\archive-v0\x\Scripts\strobes-agents-mcp.exe", True),
    ("/Users/a/.local/bin/strobes-agents-mcp", False),
])
def test_ephemeral_install_detection(exe, ephemeral):
    assert cli._is_ephemeral(exe) is ephemeral



@pytest.mark.parametrize("existing", [
    '[mcp_servers]\nstrobes-agents = { command = "a" }\n',
    'mcp_servers.strobes-agents.command = "a"\n',
    '[mcp_servers."strobes-agents"]\ncommand = "a"\n',
])
def test_codex_detects_every_toml_form(home, existing):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(existing)
    assert cli.install("codex", CMD, {}, "user", False) == 1
    assert path.read_text() == existing


def test_codex_appended_config_is_valid_toml(home):
    tomllib = pytest.importorskip("tomllib")
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "o5"\n[mcp_servers.other]\ncommand = "x"\n')
    assert cli.install("codex", CMD, {"STROBES_PROFILE": "eu"}, "user", False) == 0
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["strobes-agents"]["args"] == CMD[1:]
    assert data["mcp_servers"]["strobes-agents"]["env"] == {"STROBES_PROFILE": "eu"}
