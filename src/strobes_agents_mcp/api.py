"""Async client for the Strobes API (public GraphQL + agents REST), MasterKey auth.

Self-contained (httpx only) so the server installs anywhere with ``uvx``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from . import __version__
from .config import Profile

log = logging.getLogger(__name__)
USER_AGENT = f"strobes-agents-mcp/{__version__}"
_RETRY_STATUS = {502, 503, 504}


class StrobesError(RuntimeError):
    """An API failure with a message fit to show the model / user."""


class AuthError(StrobesError):
    """Missing, invalid or insufficient credentials."""


NOT_SIGNED_IN = (
    "Not signed in to Strobes. Call the `strobes_login` tool (opens a browser "
    "sign-in), or run `strobes-agents-mcp login` in a terminal. CI/containers can "
    "set STROBES_URL, STROBES_ORGANIZATION_ID and STROBES_API_KEY instead."
)


ENV_INCOMPLETE = (
    "STROBES_API_KEY is set but STROBES_URL and/or STROBES_ORGANIZATION_ID aren't, so it "
    "isn't used (an env key is never combined with a stored profile's URL/org). Set all "
    "three, or unset STROBES_API_KEY to use your browser sign-in.")


class StrobesAPI:
    def __init__(self, profile: Profile, *, timeout: float = 60.0):
        if not profile.complete:
            raise AuthError(ENV_INCOMPLETE if profile.source == "env-incomplete" else NOT_SIGNED_IN)
        self.profile = profile
        headers = {
            "Authorization": f"token {profile.master_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if profile.tenant_host:
            headers["Host"] = profile.tenant_host
        self._http = httpx.AsyncClient(headers=headers, verify=profile.verify_tls,
                                       timeout=httpx.Timeout(timeout, connect=15.0),
                                       follow_redirects=False)

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """Send an idempotent request, retrying transient network/gateway failures."""
        delay = 0.5
        for attempt in range(3):
            try:
                r = await self._http.request(method, url, **kw)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt == 2:
                    raise StrobesError(f"Couldn't reach {self.profile.web_url}: "
                                       f"{type(e).__name__}: {e}") from e
            else:
                if r.status_code not in _RETRY_STATUS or attempt == 2:
                    return r
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    def _auth_failure(self, r: httpx.Response, *, mutation: str | None = None) -> AuthError:
        where = self.profile.web_url
        if r.status_code == 401:
            return AuthError(f"Strobes rejected the API key for {where} (HTTP 401) — it "
                             "may be revoked or expired. Call `strobes_login` to sign in again.")
        hint = ""
        if mutation:
            hint = (f" The `{mutation}` operation may not be enabled for API keys on "
                    "this Strobes version, or your role lacks access.")
        return AuthError(f"Strobes denied the request at {where} (HTTP 403).{hint} If the "
                         "key belongs to another organization, call `strobes_login`.")

    # --------------------------------------------------------------- GraphQL
    async def graphql(self, query: str, variables: dict[str, Any] | None = None,
                      *, op: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        variables = {k: v for k, v in (variables or {}).items() if v is not None}
        is_mutation = query.lstrip().startswith("mutation")
        r = await self._send_gql(query, variables, is_mutation, timeout)
        if r.status_code in (401, 403):
            raise self._auth_failure(r, mutation=op if is_mutation else None)
        if 300 <= r.status_code < 400:
            raise StrobesError(_redirect_msg(r, self.profile.graphql_url))
        if r.status_code >= 400:
            raise StrobesError(f"Strobes API error (HTTP {r.status_code}) for "
                               f"{op or 'GraphQL request'}: {r.text[:300]}")
        try:
            body = r.json()
        except ValueError as e:
            raise StrobesError(f"Strobes returned a non-JSON response for {op}: "
                               f"{r.text[:200]}") from e
        errors = body.get("errors") or []
        if errors:
            # Never return partial data: a failed field often comes back as an
            # empty-looking wrapper (e.g. {"objects": null}), which would read as
            # "no findings" instead of "the query failed".
            raise StrobesError(_gql_message(errors, op))
        return body.get("data") or {}

    async def _send_gql(self, query: str, variables: dict[str, Any],
                        is_mutation: bool, timeout: float | None = None) -> httpx.Response:
        payload = {"query": query, "variables": variables}
        if timeout is not None:
            # slow server-side work (e.g. building a zip): one long attempt, no retries
            try:
                return await self._http.post(self.profile.graphql_url, json=payload,
                                             timeout=httpx.Timeout(timeout, connect=15.0))
            except httpx.HTTPError as e:
                raise StrobesError(f"Strobes took too long or failed ({type(e).__name__}). "
                                   "Try a smaller folder.") from e
        if is_mutation:
            # never retry a mutation on a timeout — it may have been applied
            try:
                return await self._http.post(self.profile.graphql_url, json=payload)
            except httpx.HTTPError as e:
                raise StrobesError(f"Request to {self.profile.web_url} failed: "
                                   f"{type(e).__name__}: {e}. It may or may not have "
                                   "been applied — check before retrying.") from e
        return await self._send("POST", self.profile.graphql_url, json=payload)

    # ------------------------------------------------------------------ REST
    def org_path(self, path: str) -> str:
        return f"{self.profile.rest_url}/organizations/{self.profile.org_id}/{path.lstrip('/')}"

    async def rest(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: Any = None, content: bytes | None = None,
                   headers: dict[str, str] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.org_path(path)
        if method == "GET":
            r = await self._send("GET", url, params=params, headers=headers)
        else:
            try:
                r = await self._http.request(method, url, params=params, json=json,
                                             content=content, headers=headers)
            except httpx.HTTPError as e:
                raise StrobesError(f"Request to {self.profile.web_url} failed: {e}") from e
        if r.status_code in (401, 403):
            raise self._auth_failure(r)
        if 300 <= r.status_code < 400:
            raise StrobesError(_redirect_msg(r, url))
        if r.status_code == 404 and "<html" in r.text[:200].lower():
            raise StrobesError(f"This Strobes server doesn't support {path} yet.")
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400:
            msg = (body or {}).get("error") if isinstance(body, dict) else None
            raise StrobesError(msg or f"Strobes API error (HTTP {r.status_code}) for {path}")
        return body


def _redirect_msg(r: httpx.Response, url: str) -> str:
    where = r.headers.get("location") or "?"
    return (f"Strobes answered HTTP {r.status_code} (redirect to {where}) for {url}. "
            "The configured URL is probably wrong (e.g. http vs https, or a missing /api) — "
            "sign in again with the URL you use in the browser.")


def _gql_message(errors: list[Any], op: str | None) -> str:
    msgs = []
    for e in errors:
        m = e.get("message", str(e)) if isinstance(e, dict) else str(e)
        if m not in msgs:
            msgs.append(m)
    text = "; ".join(msgs) or "unknown GraphQL error"
    if "Cannot query field" in text or "Unknown argument" in text:
        text += (" (this Strobes server is older than strobes-agents-mcp expects; "
                 "the operation isn't available there)")
    return f"{op + ': ' if op else ''}{text}"
