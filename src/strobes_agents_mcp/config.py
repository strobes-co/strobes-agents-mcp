"""Credentials & connection settings.

Credentials are shared with the Strobes agents CLI (``strobes login``): both
read and write the same ``config.json`` profile store, so signing in once with
either tool configures the other. Resolution order for each setting:

1. Environment variables (CI / containers / explicit harness config).
2. The selected profile in the shared config file.

The config file lives where the CLI keeps it (Rust ``dirs::config_dir()``):

* macOS   ``~/Library/Application Support/strobes-ai/config.json``
* Linux   ``$XDG_CONFIG_HOME/strobes-ai/config.json`` (``~/.config/…``)
* Windows ``%APPDATA%\\strobes-ai\\config.json``

``STROBES_AI_HOME`` overrides the directory (same as the CLI).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_LOGIN_URL = "https://app.strobes.co"
DEFAULT_PROFILE = "default"


class ConfigError(RuntimeError):
    """Raised when there are no usable credentials."""


# --------------------------------------------------------------------------
# environment helpers
# --------------------------------------------------------------------------
def _env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v is not None and v.strip():
            return v.strip()
    return None


def _env_bool(*names: str) -> bool | None:
    v = _env(*names)
    if v is None:
        return None
    return v.lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# config file location / IO
# --------------------------------------------------------------------------
def config_dir() -> Path:
    home = _env("STROBES_AI_HOME")
    if home:
        return Path(home).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "strobes-ai"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_raw(*, strict: bool = False) -> dict[str, Any]:
    """The whole config document (unknown keys preserved for the CLI).

    Reads are forgiving (a broken file looks empty). ``strict=True`` is for
    read-modify-write: it refuses to proceed on a file that exists but doesn't
    parse, so a save can never replace someone's profiles with ``{}``.
    """
    path = config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        if strict:
            raise ConfigError(f"Can't read {path}: {e}") from e
        return {}
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        if strict:
            raise ConfigError(f"{path} isn't valid JSON ({e}). Fix or remove it, then "
                              "sign in again — not overwriting it.") from e
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ConfigError(f"{path} doesn't contain a JSON object — not overwriting it.")
        return {}
    return data


@contextmanager
def _locked():
    """Serialize read-modify-write of the config with other writers (POSIX flock).

    The Rust CLI doesn't take this lock, but it does keep concurrent MCP servers
    (several harness windows) from losing each other's updates.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows
        yield
        return
    with open(path.parent / ".config.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def editing():
    """``with editing() as raw: ...`` — locked, strict load; saved on clean exit."""
    with _locked():
        raw = load_raw(strict=True)
        yield raw
        save_raw(raw)


def save_raw(data: dict[str, Any]) -> None:
    """Atomically write the config document, owner-readable only (it holds keys)."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if u and "://" not in u:
        u = f"https://{u}"
    return u


def api_root_for(base_url: str, deployment: str = "") -> str:
    """API root (…/api) for a profile base URL.

    Profiles written by browser login store the API root itself
    (``https://acme.strobes.co/api``); older/manual profiles store the bare
    origin, in which case the proxy serves the API under ``/api``.
    """
    base = normalize_url(base_url)
    if base.endswith("/api") or deployment in ("direct", "v1"):
        return base
    return f"{base}/api"


def web_url_for(base_url: str) -> str:
    base = normalize_url(base_url)
    return base[: -len("/api")] if base.endswith("/api") else base


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------
@dataclass
class Profile:
    """Resolved connection settings for one Strobes organization."""

    name: str = DEFAULT_PROFILE
    base_url: str = ""
    org_id: str = ""
    master_key: str = ""
    deployment: str = ""
    verify_tls: bool = True
    workspace_id: str | None = None
    email: str | None = None
    login_url: str | None = None
    tenant_host: str | None = None  # Host header override (local/dev)
    source: str = "none"  # "env", "config", "env+config", "request" or "none"
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def complete(self) -> bool:
        return bool(self.base_url and self.org_id and self.master_key)

    @property
    def api_root(self) -> str:
        return api_root_for(self.base_url, self.deployment)

    @property
    def graphql_url(self) -> str:
        return f"{self.api_root}/public/graphql/"

    @property
    def rest_url(self) -> str:
        return f"{self.api_root}/v1"

    @property
    def web_url(self) -> str:
        return web_url_for(self.base_url)

    @property
    def host(self) -> str:
        return urlparse(normalize_url(self.base_url)).hostname or ""

    def app_url(self, path: str) -> str:
        """Deep link into the web app, e.g. app_url("findings/42")."""
        app = os.getenv("STROBES_WEB_APP", "ctem")
        return f"{self.web_url}/organizations/{self.org_id}/apps/{app}/{path.lstrip('/')}"

    def redacted(self) -> dict[str, Any]:
        return {
            "profile": self.name,
            "url": self.web_url or None,
            "organization_id": self.org_id or None,
            "api_key": redact(self.master_key),
            "email": self.email,
            "default_workspace_id": self.workspace_id,
            "verify_tls": self.verify_tls,
            "credential_source": self.source,
        }


def redact(secret: str | None) -> str:
    if not secret:
        return "(unset)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"


def current_profile_name(raw: dict[str, Any] | None = None) -> str:
    env = _env("STROBES_PROFILE", "STROBES_AI_PROFILE")
    if env:
        return env
    raw = load_raw() if raw is None else raw
    return raw.get("current_profile") or DEFAULT_PROFILE


def list_profiles() -> list[dict[str, Any]]:
    raw = load_raw()
    cur = current_profile_name(raw)
    out = []
    for name, p in sorted((raw.get("profiles") or {}).items()):
        if not isinstance(p, dict):
            continue
        out.append({
            "profile": name,
            "current": name == cur,
            "url": web_url_for(p.get("base_url", "")) or None,
            "organization_id": p.get("org_id") or None,
            "email": p.get("email"),
            "signed_in": bool(p.get("base_url") and p.get("org_id") and p.get("master_key")),
        })
    return out


def _env_base_url() -> str | None:
    url = _env("STROBES_BASE_URL", "STROBES_AI_BASE_URL", "STROBES_URL")
    if url:
        return url
    host = _env("STROBES_HOST")
    return f"https://{host}" if host else None


def _env_key() -> str | None:
    return _env("STROBES_API_TOKEN", "STROBES_API_KEY", "STROBES_MASTER_KEY",
                "STROBES_AI_MASTER_KEY")


def resolve_profile(name: str | None = None) -> Profile:
    """Credentials for a profile: environment variables OR the stored profile.

    The two are never mixed field by field (that could send one org's key to
    another org's server):

    * An explicitly named profile (``name``: --profile, strobes_use_profile, a
      just-completed sign-in) always uses what's stored for it.
    * Otherwise, if an API key is set in the environment, the environment
      credential set is used; URL/org it doesn't set come from the current profile
      only when that profile has no key of its own (i.e. env-only setups).
    * Otherwise the current profile (STROBES_PROFILE or the file's default).

    TLS / Host-header / default-workspace env overrides apply in every case.
    """
    raw = load_raw()
    explicit = bool(name)
    name = name or current_profile_name(raw)
    stored = (raw.get("profiles") or {}).get(name) or {}
    if not isinstance(stored, dict):
        stored = {}

    p = Profile(
        name=name,
        base_url=stored.get("base_url", "") or "",
        org_id=stored.get("org_id", "") or "",
        master_key=stored.get("master_key", "") or "",
        deployment=stored.get("deployment", "") or "",
        verify_tls=bool(stored.get("verify_tls", True)),
        workspace_id=stored.get("workspace_id"),
        email=stored.get("email"),
        login_url=stored.get("login_url"),
        extra=stored,
    )
    if p.complete:
        p.source = "config"

    env_key = _env_key()
    if env_key and not explicit:
        file_has_key = bool(p.master_key)
        url = _env_base_url()
        org = _env("STROBES_ORGANIZATION_ID", "STROBES_ORG_ID", "STROBES_AI_ORG_ID")
        if url or file_has_key:
            p.base_url = url or ""
            p.deployment = _env("STROBES_AI_DEPLOYMENT") or ""
        if org or file_has_key:
            p.org_id = org or ""
        p.master_key = env_key
        p.email = None if file_has_key else p.email
        p.source = "env"

    tls = _env_bool("STROBES_VERIFY_TLS")
    if tls is not None:
        p.verify_tls = tls
    if _env_bool("STROBES_INSECURE"):
        p.verify_tls = False
    p.tenant_host = _env("STROBES_TENANT_HOST")
    if not p.tenant_host and _env("STROBES_BASE_URL") and _env("STROBES_HOST"):
        # Legacy: STROBES_BASE_URL dials a local server, STROBES_HOST names the tenant.
        p.tenant_host = _env("STROBES_HOST")
    ws = _env("STROBES_WORKSPACE_ID", "STROBES_AI_WORKSPACE_ID")
    if ws:
        p.workspace_id = ws
    if not p.complete:
        p.source = "env-incomplete" if (env_key and not explicit) else "none"
    return p


def save_login(profile: str, *, base_url: str, org_id: str, master_key: str,
               workspace_id: str | None, email: str | None, login_url: str,
               verify_tls: bool = True, make_current: bool = False) -> None:
    """Persist a browser-login result in the CLI-compatible profile store."""
    with editing() as raw:
        _apply_login(raw, profile, base_url=base_url, org_id=org_id, master_key=master_key,
                     workspace_id=workspace_id, email=email, login_url=login_url,
                     verify_tls=verify_tls, make_current=make_current)


def _apply_login(raw: dict[str, Any], profile: str, *, base_url: str, org_id: str,
                 master_key: str, workspace_id: str | None, email: str | None,
                 login_url: str, verify_tls: bool, make_current: bool) -> None:
    profiles = raw.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError(f"{config_path()}: 'profiles' isn't an object — not overwriting it.")
    p = profiles.get(profile) if isinstance(profiles.get(profile), dict) else {}
    same_org = p.get("org_id") == org_id
    base = normalize_url(base_url)
    p.update({
        "base_url": base,
        "deployment": "direct" if base.endswith("/api") else "",
        "org_id": org_id,
        "master_key": master_key,
        "workspace_id": workspace_id,
        "login_url": login_url,
        "email": email,
        "verify_tls": verify_tls,
    })
    if not same_org:
        p["thread_id"] = None
    profiles[profile] = p
    cur = raw.get("current_profile")
    cur_ok = isinstance(profiles.get(cur), dict) and profiles[cur].get("master_key")
    if make_current or not cur_ok:
        raw["current_profile"] = profile


def clear_login(profile: str) -> bool:
    """Forget a profile's API key (keeps its URL for the next login)."""
    with editing() as raw:
        p = (raw.get("profiles") or {}).get(profile)
        if not isinstance(p, dict) or not p.get("master_key"):
            return False
        p["master_key"] = ""
    return True


def set_current_profile(profile: str) -> None:
    with editing() as raw:
        if profile not in (raw.get("profiles") or {}):
            raise ConfigError(f"No profile named {profile!r}. Known: "
                              + ", ".join(sorted((raw.get("profiles") or {}).keys())))
        raw["current_profile"] = profile
