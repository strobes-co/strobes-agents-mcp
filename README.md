# strobes-agents-mcp

Model Context Protocol (MCP) server for **Strobes AI workspaces & agents**. Lets
Claude Code (or any MCP host) manage AI workspaces, **spin up / stop agent
tasks**, read a task's **conversation** and what agents produced (**findings, assets,
files**), and **run & control workflows** — all via the Strobes **public**
GraphQL API with a MasterKey.

Wraps [`strobes-gql-client`](https://github.com/strobes-co/strobes-gql-client),
same as the companion **[strobes-mcp](../strobes-mcp)**.

## Tools (23)
| Group | Tools |
|-------|-------|
| Connectivity | `strobes_agents_whoami`, `list_agents` |
| Workspaces | `list_workspaces`, `get_workspace`, `workspace_stats`, `create_workspace`, `archive_workspace` |
| Tasks | `list_workspace_tasks`, `create_workspace_task` (spin up), `cancel_workspace_task` (stop), `retry_workspace_task` |
| Outputs | `list_workspace_findings`, `list_workspace_assets`, `list_workspace_files`, `get_workspace_file` (pull one), `download_workspace` (zip) |
| Task conversation | `get_task_messages` (paginated messages a task produced) |
| Workflows | `list_workflow_templates`, `get_workspace_workflow`, `start_workflow`, `pause_workflow`, `resume_workflow`, `cancel_workflow` |

## Configuration (environment variables)
Identical to `strobes-mcp` — one MasterKey drives both servers.

| Variable | Required | Meaning |
|----------|----------|---------|
| `STROBES_HOST` | yes | Tenant host, e.g. `acme.strobes.co` |
| `STROBES_API_TOKEN` | yes | Strobes **MasterKey** |
| `STROBES_ORGANIZATION_ID` | yes | Organization UUID |
| `STROBES_BASE_URL` | no | Override scheme+host (self-hosted/local) |
| `STROBES_TENANT_HOST` | no | Override the `Host` header (local/dev) |
| `STROBES_VERIFY_TLS` | no | `false` to disable TLS verification |

## Install as a Claude Code plugin
```
/plugin marketplace add strobes-co/strobes-agents-mcp     # or a local path
/plugin install strobes-agents-mcp@strobes-agents
```

## Requires API support
These operations are served on the public endpoint by
`strobes/graphql/public/agents/` (re-exposing the internal workspace/pulse
resolvers) with the mutation names added to the MasterKey allowlist. A backend
predating that change will 403 the agent/workflow mutations.

## Development / testing
```
STROBES_HOST=... STROBES_API_TOKEN=... STROBES_ORGANIZATION_ID=... \
  uv run --with 'mcp<2' --with requests --with sgqlc python tests/test_e2e.py
```
