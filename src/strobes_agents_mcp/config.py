"""Runtime configuration for the Strobes Agents MCP server.

Talks to the Strobes **public** GraphQL endpoint (``/api/public/graphql/``) with
a MasterKey, exactly like strobes-mcp. Production needs ``STROBES_HOST``,
``STROBES_API_TOKEN`` and ``STROBES_ORGANIZATION_ID``; the other knobs are for
self-hosted / on-prem / local-dev deployments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    host: str
    api_token: str
    organization_id: str
    base_url: str | None = None
    tenant_host: str | None = None
    verify_tls: bool = True

    @property
    def public_graphql_url(self) -> str:
        base = (self.base_url or f"https://{self.host}").rstrip("/")
        return f"{base}/api/public/graphql/"

    @property
    def host_header(self) -> str:
        return self.tenant_host or self.host


def load_config() -> Config:
    host = _env("STROBES_HOST")
    token = _env("STROBES_API_TOKEN", "STROBES_MASTER_KEY")
    org = _env("STROBES_ORGANIZATION_ID", "STROBES_ORG_ID")
    missing = [n for n, v in (("STROBES_HOST", host), ("STROBES_API_TOKEN", token),
                              ("STROBES_ORGANIZATION_ID", org)) if not v]
    if missing:
        raise ConfigError("Missing required environment variable(s): "
                          + ", ".join(missing) + ".")
    return Config(host=host, api_token=token, organization_id=org,
                  base_url=_env("STROBES_BASE_URL"),
                  tenant_host=_env("STROBES_TENANT_HOST"),
                  verify_tls=_bool("STROBES_VERIFY_TLS", True))
