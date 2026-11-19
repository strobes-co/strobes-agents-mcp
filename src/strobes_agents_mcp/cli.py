"""Command line: run the MCP server, sign in, and wire it into MCP clients.

    strobes-agents-mcp                      run the server over stdio (what MCP hosts launch)
    strobes-agents-mcp login [--url URL]    browser sign-in (shared with the Strobes CLI)
    strobes-agents-mcp status               show who you're signed in as and test the connection
    strobes-agents-mcp install <client>     add the server to Claude Code, Cursor, VS Code, …
    strobes-agents-mcp serve --http         streamable-HTTP server for remote / shared use
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__

SERVER_NAME = "strobes-agents"
DEFAULT_PACKAGE = "git+https://github.com/strobes-co/strobes-agents-mcp"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _err(msg: str) -> None:
    print(f"\033[31m✗\033[0m {msg}" if sys.stderr.isatty() else f"error: {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"\033[32m✔\033[0m {msg}" if sys.stdout.isatty() else msg)


def launch_command(override: str | None = None) -> list[str]:
    """The command an MCP host should run to start this server."""
    if override:
        return shlex.split(override)
    exe = shutil.which("strobes-agents-mcp")
    # A persistent install (uv tool / pipx / pip) is fastest to launch. A uvx
    # run lives in a throwaway cache env, so point hosts at uvx instead.
    if exe and not _is_ephemeral(exe):
        return [exe]
    pkg = os.getenv("STROBES_MCP_PACKAGE", DEFAULT_PACKAGE)
    if pkg == "strobes-agents-mcp":
        return ["uvx", "strobes-agents-mcp"]
    return ["uvx", "--from", pkg, "strobes-agents-mcp"]


def _toml_has_server(text: str, path: Path) -> bool:
    """Whether a Codex config already defines our server, in any TOML form
    (table, quoted table, dotted key or inline table)."""
    if not text.strip():
        return False
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import re
        return bool(re.search(r"mcp_servers[\s\S]*?[\"']?" + re.escape(SERVER_NAME)
                              + r"[\"']?\s*[\]=.]", text))
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path} isn't valid TOML ({e}); fix it or add the server by hand.")
    servers = data.get("mcp_servers")
    return isinstance(servers, dict) and SERVER_NAME in servers


def _is_ephemeral(exe: str) -> bool:
    p = exe.replace("\\", "/").lower()
    markers = ("/.cache/uv/", "/archive-v0/", "/caches/uv/", "/uv/cache/", "/builds-v0/",
               "/pipx/.cache/", "/_npx/")
    if any(m in p for m in markers):
        return True
    cache = os.getenv("UV_CACHE_DIR")
    return bool(cache) and p.startswith(cache.replace("\\", "/").lower())


def _server_entry(cmd: list[str], env: dict[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": cmd[0], "args": cmd[1:]}
    if env:
        entry["env"] = env
    return entry


def _merge_json(path: Path, key: str, name: str, entry: dict[str, Any]) -> None:
    data: Any = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except ValueError as e:
            raise SystemExit(f"{path} isn't valid JSON ({e}); fix it or add the server by hand.")
    if not isinstance(data, dict) or not isinstance(data.setdefault(key, {}), dict):
        raise SystemExit(f"{path} has an unexpected shape ('{key}' should be an object); "
                         "add the server by hand.")
    data[key][name] = entry
    _atomic_write(path, json.dumps(data, indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """Replace a user's client config without ever leaving it half-written."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _app_support() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")


# --------------------------------------------------------------------------
# install targets
# --------------------------------------------------------------------------
CLIENTS = ["claude-code", "claude-desktop", "cursor", "vscode", "windsurf", "codex",
           "gemini", "json"]


def install(client: str, cmd: list[str], env: dict[str, str], scope: str,
            dry_run: bool) -> int:
    entry = _server_entry(cmd, env)

    if client == "json":
        print(json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2))
        return 0

    if client == "claude-code":
        # `-e/--env <env...>` is variadic, so the name must come before it
        argv = ["claude", "mcp", "add", "--scope", scope, SERVER_NAME]
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        argv += ["--", *cmd]
        if dry_run or not shutil.which("claude"):
            print(("Run:\n  " if not dry_run else "") + shlex.join(argv))
            return 0 if dry_run else 1
        subprocess.run(["claude", "mcp", "remove", "--scope", scope, SERVER_NAME],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run(argv)
        if r.returncode == 0:
            _ok(f"Added to Claude Code ({scope} scope). Restart Claude Code or run /mcp.")
        return r.returncode

    if client == "codex":
        path = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        block = [f"[mcp_servers.{SERVER_NAME}]", f"command = {json.dumps(cmd[0])}",
                 f"args = {json.dumps(cmd[1:])}"]
        if env:
            block.append(f"env = {{ {', '.join(f'{k} = {json.dumps(v)}' for k, v in env.items())} }}")
        text = "\n".join(block) + "\n"
        if dry_run:
            print(f"# {path}\n{text}")
            return 0
        existing = path.read_text() if path.exists() else ""
        if _toml_has_server(existing, path):
            _err(f"{path} already has [mcp_servers.{SERVER_NAME}] — edit it by hand:\n{text}")
            return 1
        _atomic_write(path, existing + ("\n" if existing and not existing.endswith("\n") else "")
                      + ("\n" if existing else "") + text)
        _ok(f"Added to Codex ({path}).")
        return 0

    if client == "vscode":
        vs_entry = {"type": "stdio", **entry}
        if scope == "project":
            path, key = Path.cwd() / ".vscode" / "mcp.json", "servers"
        else:
            code = shutil.which("code")
            if code and not dry_run:
                r = subprocess.run([code, "--add-mcp", json.dumps({"name": SERVER_NAME, **vs_entry})])
                if r.returncode == 0:
                    _ok("Added to VS Code (user). Open Copilot Chat → Agent mode → tools.")
                return r.returncode
            print(json.dumps({"servers": {SERVER_NAME: vs_entry}}, indent=2))
            return 0
        if dry_run:
            print(f"# {path}\n" + json.dumps({key: {SERVER_NAME: vs_entry}}, indent=2))
            return 0
        _merge_json(path, key, SERVER_NAME, vs_entry)
        _ok(f"Added to {path}.")
        return 0

    paths = {
        "claude-desktop": (_app_support() / "Claude" / "claude_desktop_config.json", "mcpServers"),
        "cursor": ((Path.cwd() / ".cursor" / "mcp.json") if scope == "project"
                   else (Path.home() / ".cursor" / "mcp.json"), "mcpServers"),
        "windsurf": (Path.home() / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers"),
        "gemini": ((Path.cwd() / ".gemini" / "settings.json") if scope == "project"
                   else (Path.home() / ".gemini" / "settings.json"), "mcpServers"),
    }
    path, key = paths[client]
    if dry_run:
        print(f"# {path}\n" + json.dumps({key: {SERVER_NAME: entry}}, indent=2))
        return 0
    _merge_json(path, key, SERVER_NAME, entry)
    _ok(f"Added to {client} ({path}). Restart {client} to load it.")
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_login(a: argparse.Namespace) -> int:
    from .auth import LoginError, start_login, wait_for_login
    from .config import resolve_profile

    current = resolve_profile(a.profile)

    async def run() -> int:
        try:
            login = await start_login(a.url or current.login_url, profile=current.name,
                                      verify_tls=not a.insecure and current.verify_tls,
                                      launch_browser=not a.no_browser)
        except LoginError as e:
            _err(str(e))
            return 1
        print()
        print("  " + ("Opened your browser to sign in." if login.browser_opened
                      else "Open this link to sign in:"))
        print(f"  {login.verification_url}\n")
        print(f"  Confirm the code matches:  {login.user_code}")
        print("  Then pick your organization (and optionally a workspace) and click Authorize.\n",
              flush=True)
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0

        def tick(_):
            nonlocal i
            if sys.stdout.isatty():
                print(f"\r  {spin[i % len(spin)]} Waiting for approval… (Ctrl-C to cancel)",
                      end="", flush=True)
            i += 1

        try:
            await wait_for_login(login, on_tick=tick)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print()
            _err("Sign-in cancelled.")
            return 130
        if sys.stdout.isatty():
            print("\r\033[2K", end="")
        if login.status != "approved":
            _err(login.error or f"Sign-in {login.status}.")
            return 1
        r = login.result
        who = f" as {r['email']}" if r.get("email") else ""
        ws = f" · workspace {r['workspace_name']}" if r.get("workspace_name") else ""
        _ok(f"Signed in{who}{ws} (profile '{login.profile}').")
        from .config import current_profile_name
        if current_profile_name() != login.profile:
            print(f"  '{current_profile_name()}' is still the default — run "
                  f"`strobes-agents-mcp use {login.profile}` to switch.")
        print("  Next: strobes-agents-mcp install claude-code   (or cursor, vscode, codex, …)")
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


def cmd_logout(a: argparse.Namespace) -> int:
    from .config import clear_login, resolve_profile

    name = a.profile or resolve_profile().name
    if clear_login(name):
        _ok(f"Signed out of profile '{name}'.")
    else:
        print(f"Profile '{name}' wasn't signed in.")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    from .api import StrobesAPI, StrobesError
    from .config import config_path, list_profiles, resolve_profile
    from . import queries as Q

    p = resolve_profile(a.profile)
    info: dict[str, Any] = {"version": __version__, "config_file": str(config_path()),
                            **p.redacted()}
    code = 0
    if not p.complete:
        info["signed_in"] = False
        info["hint"] = "Run: strobes-agents-mcp login"
        code = 1
    else:
        async def check():
            api = StrobesAPI(p)
            try:
                await api.graphql(Q.WORKSPACES, {"org": p.org_id, "limit": 1}, op="workspaces")
                return None
            except StrobesError as e:
                return str(e)
            finally:
                await api.aclose()
        err = asyncio.run(check())
        info["signed_in"] = True
        info["connected"] = err is None
        if err:
            info["error"] = err
            code = 1
    info["profiles"] = list_profiles()
    if a.json:
        print(json.dumps(info, indent=2))
    else:
        for k, v in info.items():
            if k == "profiles":
                if len(v) > 1:
                    print("profiles:")
                    for pr in v:
                        mark = "*" if pr["current"] else " "
                        print(f"  {mark} {pr['profile']:<12} {pr.get('url') or '-':<40} "
                              f"{'signed in' if pr['signed_in'] else 'signed out'}")
                continue
            if v is not None:
                print(f"{k:>22}: {v}")
    return code


def cmd_profiles(a: argparse.Namespace) -> int:
    from .config import list_profiles

    rows = list_profiles()
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No profiles yet. Run: strobes-agents-mcp login")
    for pr in rows:
        mark = "*" if pr["current"] else " "
        print(f"{mark} {pr['profile']:<12} {pr.get('url') or '-':<40} "
              f"{pr.get('email') or '':<28} {'signed in' if pr['signed_in'] else 'signed out'}")
    return 0


def cmd_use(a: argparse.Namespace) -> int:
    from .config import ConfigError, set_current_profile

    try:
        set_current_profile(a.profile)
    except ConfigError as e:
        _err(str(e))
        return 1
    _ok(f"Now using profile '{a.profile}' (also the Strobes CLI default).")
    return 0


def cmd_install(a: argparse.Namespace) -> int:
    from .config import resolve_profile

    cmd = launch_command(a.command)
    env: dict[str, str] = {}
    if a.profile:
        env["STROBES_PROFILE"] = a.profile
    p = resolve_profile(a.profile)
    if not p.complete and not a.dry_run:
        print("Note: you're not signed in yet — run `strobes-agents-mcp login`, or call "
              "the strobes_login tool from your agent.\n")
    targets = CLIENTS[:-1] if a.client == "all" else [a.client]
    rc = 0
    for c in targets:
        if a.client == "all":
            print(f"— {c}")
        rc |= install(c, cmd, env, a.scope, a.dry_run)
    return rc


def detect_clients() -> list[str]:
    """MCP clients that look installed on this machine."""
    home = Path.home()
    found = []
    if shutil.which("claude"):
        found.append("claude-code")
    if (_app_support() / "Claude").is_dir():
        found.append("claude-desktop")
    if (home / ".cursor").is_dir():
        found.append("cursor")
    if shutil.which("code"):
        found.append("vscode")
    if (home / ".codeium" / "windsurf").is_dir():
        found.append("windsurf")
    if shutil.which("codex") or (home / ".codex").is_dir():
        found.append("codex")
    if shutil.which("gemini") or (home / ".gemini").is_dir():
        found.append("gemini")
    return found


def cmd_setup(a: argparse.Namespace) -> int:
    """Sign in if needed, then add the server to the chosen / detected clients."""
    from .config import resolve_profile

    bad = [c for c in a.clients if c not in CLIENTS[:-1]]
    if bad:
        _err(f"Unknown client(s): {', '.join(bad)}. Choose from: {', '.join(CLIENTS[:-1])}.")
        return 2
    if not resolve_profile(a.profile).complete:
        print("Step 1 — sign in to Strobes")
        rc = cmd_login(argparse.Namespace(url=a.url, profile=a.profile,
                                          no_browser=a.no_browser, insecure=False))
        if rc:
            return rc
    else:
        p = resolve_profile(a.profile)
        _ok(f"Already signed in to {p.web_url}" + (f" as {p.email}" if p.email else ""))
    clients = a.clients or detect_clients()
    if not clients:
        print("\nNo MCP clients detected. Add this to your client's MCP config:\n")
        return install("json", launch_command(a.command), {}, "user", False)
    print(f"\nStep 2 — adding to: {', '.join(clients)}")
    env = {"STROBES_PROFILE": a.profile} if a.profile else {}
    rc = 0
    for c in clients:
        rc |= install(c, launch_command(a.command), env, "user", False)
    if not rc:
        print("\nDone. In your agent, try: \"list my Strobes workspaces\".")
    return rc


def cmd_serve(a: argparse.Namespace) -> int:
    import logging

    logging.basicConfig(level=os.getenv("STROBES_LOG_LEVEL", "WARNING").upper(),
                        stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.profile:
        os.environ["STROBES_PROFILE"] = a.profile
    transport = "streamable-http" if a.http else "stdio"
    if a.http and a.require_header_auth:
        os.environ["STROBES_MCP_REQUIRE_HEADER_AUTH"] = "1"
    if a.http and not a.require_header_auth and a.host in ("127.0.0.1", "localhost", "::1"):
        # single-user HTTP on loopback (DNS-rebinding protection stays on): the
        # caller is this machine's user, so sign-in tools are fine
        os.environ["STROBES_MCP_LOCAL_HTTP"] = "1"
    from .server import build

    server = build(transport)
    if a.http:
        from mcp.server.transport_security import TransportSecuritySettings

        server.settings.host = a.host
        server.settings.port = a.port
        server.settings.stateless_http = True
        loopback = a.host in ("127.0.0.1", "localhost", "::1")
        if a.allowed_host:
            # bare host (standard 80/443 port — what browsers & LBs send) and host:port
            hosts: list[str] = []
            for h in a.allowed_host:
                hosts += [h] if ":" in h else [h, f"{h}:*"]
            server.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                allowed_origins=[f"{sch}://{h}" for h in hosts for sch in ("http", "https")])
        elif not loopback:
            # Behind a load balancer / in a container the Host header is the public
            # name, which the localhost-only default would reject (HTTP 421).
            server.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False)
            print("note: Host-header checks are off for a non-localhost bind; pass "
                  "--allowed-host <public-name> to enable them.", file=sys.stderr)
        if not loopback and not a.require_header_auth:
            print("WARNING: serving this machine's stored Strobes login to the network. "
                  "Use --require-header-auth unless every client is trusted.", file=sys.stderr)
        print(f"strobes-agents-mcp {__version__} — streamable HTTP on "
              f"http://{a.host}:{a.port}/mcp", file=sys.stderr)
    server.run(transport=transport)
    return 0


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="strobes-agents-mcp",
        description="Strobes AI agents MCP server. With no command, serves MCP over stdio.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="Run the MCP server (default: stdio).")
    s.add_argument("--http", action="store_true", help="Serve streamable HTTP instead of stdio.")
    s.add_argument("--host", default=os.getenv("STROBES_MCP_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.getenv("STROBES_MCP_PORT", "8765")))
    s.add_argument("--require-header-auth", action="store_true",
                   help="HTTP: only accept credentials sent per request (Authorization: "
                        "Bearer <MasterKey>); never use this machine's stored login.")
    s.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                   help="HTTP: public host name(s) clients use (enables Host-header "
                        "checks on a non-localhost bind). Repeatable.")
    s.add_argument("--profile", help="Profile to use (default: current).")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("login", help="Sign in through the browser.")
    s.add_argument("--url", help="Strobes URL (default: last used, else https://app.strobes.co).")
    s.add_argument("--profile", help="Save under this profile name.")
    s.add_argument("--no-browser", action="store_true", help="Just print the link.")
    s.add_argument("--insecure", action="store_true", help="Skip TLS verification (dev only).")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("logout", help="Forget the stored API key.")
    s.add_argument("--profile")
    s.set_defaults(fn=cmd_logout)

    s = sub.add_parser("status", aliases=["whoami"], help="Show sign-in & test the connection.")
    s.add_argument("--profile")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("profiles", help="List signed-in organizations/profiles.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_profiles)

    s = sub.add_parser("use", help="Switch the default profile.")
    s.add_argument("profile")
    s.set_defaults(fn=cmd_use)

    s = sub.add_parser("setup", help="Sign in (if needed) and add the server to your MCP clients.")
    s.add_argument("clients", nargs="*", metavar="client", help=f"Any of: {', '.join(CLIENTS[:-1])} (default: detected).")
    s.add_argument("--url", help="Strobes URL for sign-in.")
    s.add_argument("--profile")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--command", help="Override the launch command.")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("install", help="Add this server to an MCP client.")
    s.add_argument("client", choices=CLIENTS + ["all"])
    s.add_argument("--scope", choices=["user", "project", "local"], default="user",
                   help="Claude Code: user/project/local. Cursor/VS Code/Gemini: user or project.")
    s.add_argument("--profile", help="Pin the client to this profile (sets STROBES_PROFILE).")
    s.add_argument("--command", help="Override the launch command (default: auto-detected).")
    s.add_argument("--dry-run", action="store_true", help="Print the config instead of writing it.")
    s.set_defaults(fn=cmd_install)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        a = ap.parse_args(["serve", *(argv or sys.argv[1:])])
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
