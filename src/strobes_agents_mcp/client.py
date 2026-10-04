"""Thin wrapper around ``strobes_gql_client`` for AI workspace & agent ops.

These operations are served on the Strobes **public** GraphQL endpoint by
re-exposing the internal workspace/pulse resolvers (MasterKey auth). The client
pins their field selections, so we just call ``execute_query`` /
``execute_mutation`` by name.
"""

from __future__ import annotations

from functools import lru_cache

import requests
from sgqlc.endpoint.requests import RequestsEndpoint
from strobes_gql_client.client import StrobesGQLClient

from .config import Config, load_config


def build_client(cfg: Config) -> StrobesGQLClient:
    client = StrobesGQLClient(host=cfg.host, api_token=cfg.api_token,
                              verify=cfg.verify_tls)
    if cfg.base_url or cfg.tenant_host or not cfg.verify_tls:
        session = requests.Session()
        session.verify = cfg.verify_tls
        headers = dict(client.headers)
        headers["Host"] = cfg.host_header
        client.graphql_url = cfg.public_graphql_url
        client.endpoint = RequestsEndpoint(cfg.public_graphql_url, headers,
                                           session=session)
    return client


@lru_cache(maxsize=1)
def get_context() -> tuple[StrobesGQLClient, Config]:
    cfg = load_config()
    return build_client(cfg), cfg
