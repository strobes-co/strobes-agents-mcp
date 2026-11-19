"""Browser sign-in (OAuth-style device flow), same protocol as ``strobes login``.

1. POST <url>/api/v1/cli/auth/device/        → device_code, user_code, verification_path
2. The user opens <web>/cli/login?code=<user_code>, picks org + workspace, authorizes.
3. POST <url>/api/v1/cli/auth/device/token/  (poll) → organization_id, api_base_url,
   master_key, workspace_id, email

Server side: strobes/agents/views/cli_auth_views.py. The device_code never
leaves this process; the user_code is only for the user to compare by eye.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import socket
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from . import __version__
from .config import DEFAULT_LOGIN_URL, normalize_url, save_login

USER_AGENT = f"strobes-agents-mcp/{__version__}"


class LoginError(RuntimeError):
    pass


def _api_v1(url: str) -> str:
    u = normalize_url(url)
    return f"{u}/v1" if u.endswith("/api") else f"{u}/api/v1"


def _web(url: str) -> str:
    u = normalize_url(url)
    return u[: -len("/api")] if u.endswith("/api") else u


def client_name(harness: str | None = None) -> str:
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001
        who = ""
    host = socket.gethostname().split(".")[0]
    where = f"{who}@{host}" if who and host else (host or who)
    label = "Strobes MCP" + (f" · {harness}" if harness else "")
    return f"{label} ({where})" if where else label


def open_browser(url: str) -> bool:
    if os.getenv("STROBES_NO_BROWSER") or os.getenv("SSH_CONNECTION") or os.getenv("CI"):
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001
        return False


@dataclass
class DeviceLogin:
    """One in-flight sign-in."""

    url: str
    profile: str
    verify_tls: bool
    device_code: str
    user_code: str
    verification_url: str
    interval: float
    expires_at: float
    status: str = "pending"  # pending | approved | denied | expired | error
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    browser_opened: bool = False

    @property
    def expires_in(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "profile": self.profile}
        if self.status == "pending":
            out.update({
                "verification_url": self.verification_url,
                "user_code": self.user_code,
                "expires_in_seconds": self.expires_in,
                "browser_opened": self.browser_opened,
            })
        if self.status == "approved":
            out.update({
                "email": self.result.get("email"),
                "organization_id": self.result.get("organization_id"),
                "workspace": self.result.get("workspace_name"),
                "url": _web(self.result.get("api_base_url") or self.url),
            })
        if self.error:
            out["error"] = self.error
        return out


def _http(verify: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=verify, timeout=20.0,
                             headers={"User-Agent": USER_AGENT})


async def start_login(url: str | None = None, *, profile: str = "default",
                      verify_tls: bool = True, harness: str | None = None,
                      web_url: str | None = None,
                      launch_browser: bool = True) -> DeviceLogin:
    url = normalize_url(url or DEFAULT_LOGIN_URL)
    api = _api_v1(url)
    web = normalize_url(web_url or os.getenv("STROBES_WEB_URL") or _web(url))
    async with _http(verify_tls) as http:
        try:
            r = await http.post(f"{api}/cli/auth/device/",
                                json={"client_name": client_name(harness)})
        except httpx.HTTPError as e:
            raise LoginError(f"Couldn't reach {url}: {e}") from e
    if r.status_code == 404:
        raise LoginError(f"{url} doesn't support browser sign-in yet. Set "
                         "STROBES_URL, STROBES_ORGANIZATION_ID and STROBES_API_KEY "
                         "(a MasterKey from Settings → API access) instead.")
    if r.status_code >= 400:
        raise LoginError(f"Couldn't start sign-in at {url} (HTTP {r.status_code}).")
    body = r.json()
    login = DeviceLogin(
        url=url, profile=profile, verify_tls=verify_tls,
        device_code=body["device_code"], user_code=body["user_code"],
        verification_url=f"{web}{body.get('verification_path') or '/cli/login?code=' + body['user_code']}",
        interval=max(1.0, float(body.get("interval") or 2)),
        expires_at=time.time() + float(body.get("expires_in") or 600),
    )
    if launch_browser:
        login.browser_opened = open_browser(login.verification_url)
    return login


async def poll_once(login: DeviceLogin, http: httpx.AsyncClient) -> bool:
    """One token poll. Returns True when the login reached a final state."""
    if login.status != "pending":
        return True
    if time.time() >= login.expires_at:
        login.status, login.error = "expired", "Sign-in expired — start it again."
        return True
    try:
        r = await http.post(f"{_api_v1(login.url)}/cli/auth/device/token/",
                            json={"device_code": login.device_code})
    except httpx.HTTPError:
        return False  # transient network error: keep waiting
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code < 300 and isinstance(body, dict):
        # file lock + disk write: keep them off the event loop
        await asyncio.to_thread(_finish, login, body)
        return True
    err = body.get("error") if isinstance(body, dict) else ""
    err = err or ""
    if err == "slow_down":  # RFC 8628 §3.5: back off by 5 seconds
        login.interval += 5
        return False
    if err in ("authorization_pending", ""):
        return False
    if err == "access_denied":
        login.status, login.error = "denied", "Sign-in was denied in the browser."
    elif err == "expired_token":
        login.status, login.error = "expired", "Sign-in expired — start it again."
    else:
        login.status, login.error = "error", f"Sign-in failed: {err}"
    return True


def _finish(login: DeviceLogin, creds: dict[str, Any]) -> None:
    api_base = creds.get("api_base_url") or _api_v1(login.url)[: -len("/v1")]
    if not creds.get("master_key") or not creds.get("organization_id"):
        login.status, login.error = "error", "Server returned incomplete credentials."
        return
    save_login(login.profile, base_url=api_base, org_id=creds["organization_id"],
               master_key=creds["master_key"], workspace_id=creds.get("workspace_id"),
               email=creds.get("email"), login_url=login.url,
               verify_tls=login.verify_tls)
    login.result = {k: v for k, v in creds.items() if k != "master_key"}
    login.result["api_base_url"] = api_base
    login.status = "approved"


async def wait_for_login(login: DeviceLogin, timeout: float | None = None,
                         on_tick: Callable[[DeviceLogin], Any] | None = None) -> DeviceLogin:
    """Poll until the login is final, or ``timeout`` passes (default: until it expires)."""
    deadline = min(time.time() + timeout, login.expires_at) if timeout is not None \
        else login.expires_at
    try:
        async with _http(login.verify_tls) as http:
            while True:
                if await poll_once(login, http):
                    return login
                if on_tick:
                    on_tick(login)
                now = time.time()
                if now >= login.expires_at or (timeout is not None and now >= deadline):
                    break
                await asyncio.sleep(max(0.0, min(login.interval, deadline - now)))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — surface, never leave it "pending"
        login.status, login.error = "error", f"Sign-in failed: {type(e).__name__}: {e}"
        return login
    if login.status == "pending" and time.time() >= login.expires_at:
        login.status, login.error = "expired", "Sign-in expired — start it again."
    return login
