# strobes-agents-mcp

MCP server for **Strobes AI agents**. Lets Claude Code, Cursor, VS Code, Codex, Gemini
CLI, Claude Desktop, or any other MCP host:

- **spin up agents** (web/API/cloud pentest, recon, triage, reporting …) on a task and
  **follow them to a result**, with live progress
- read what agents produced: **findings, assets, files, reports**
- **upload context** (scope docs, API specs) for agents to use
- launch and steer **multi-phase workflows** (e.g. *Web App Pentest — Standard*)
- see pending **approvals** and **AI credit** usage

Sign-in is a **browser approval, like `strobes login`**. You don't paste API keys, and one
login covers both this server and the Strobes CLI.

---

## Quick start

**Claude Code (plugin):**
```
/plugin marketplace add strobes-co/strobes-agents-mcp
/plugin install strobes-agents-mcp@strobes-agents
/strobes-agents-mcp:login
```
The plugin brings the server, a skill, and slash commands
(`/strobes-agents-mcp:login`, `:agent <task>`, `:status`).

**Any MCP client, one command** (needs [uv](https://docs.astral.sh/uv/)):
```bash
uvx --from git+https://github.com/strobes-co/strobes-agents-mcp strobes-agents-mcp setup
```
`setup` opens the browser to sign you in, then adds the server to every client it finds
on your machine. You can also name the clients:
`… setup claude-code cursor codex`.

**Or add it by hand.** You don't need any env vars, because the server reads the shared
login:
```bash
claude mcp add strobes-agents -- uvx --from git+https://github.com/strobes-co/strobes-agents-mcp strobes-agents-mcp
```
Then ask your agent to *"sign in to Strobes"*. It calls `strobes_login`, shows you a
link and code, and waits for you to approve.

> Prefer a persistent install (faster start-up)?
> `uv tool install git+https://github.com/strobes-co/strobes-agents-mcp`, then
> `strobes-agents-mcp setup`.

### Per-client config

`strobes-agents-mcp install <client>` writes the right file for you (`--dry-run` prints
it instead):

| Client | Command | Writes |
|---|---|---|
| Claude Code | `install claude-code [--scope user\|project\|local]` | via `claude mcp add` |
| Claude Desktop | `install claude-desktop` | `claude_desktop_config.json` |
| Cursor | `install cursor [--scope project]` | `~/.cursor/mcp.json` |
| VS Code (Copilot) | `install vscode [--scope project]` | `code --add-mcp` / `.vscode/mcp.json` |
| Windsurf | `install windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| Codex CLI | `install codex` | `~/.codex/config.toml` |
| Gemini CLI | `install gemini [--scope project]` | `~/.gemini/settings.json` |
| Anything else | `install json` | prints an `mcpServers` snippet |

Generic stdio config:
```json
{
  "mcpServers": {
    "strobes-agents": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/strobes-co/strobes-agents-mcp", "strobes-agents-mcp"]
    }
  }
}
```

---

## Authentication

There are three ways to authenticate. Pick whichever fits your setup.

1. **Browser sign-in (recommended).** Use `strobes-agents-mcp login [--url https://acme.strobes.co]`,
   or the `strobes_login` tool from inside your agent. This is the same device flow as
   the Strobes CLI: you confirm a code, pick the organization (and optionally a default
   workspace), and click **Authorize**. Strobes then issues *your* API key, so the agent
   acts with exactly your permissions.
2. **Shared with the Strobes CLI.** If you've already run `strobes login`, there's
   nothing to do. Both tools use the same profile store:
   - macOS `~/Library/Application Support/strobes-ai/config.json`
   - Linux `~/.config/strobes-ai/config.json`
   - Windows `%APPDATA%\strobes-ai\config.json`

   The file is written with `0600` permissions. `STROBES_AI_HOME` overrides the location.
3. **Environment variables**, for CI and containers. If `STROBES_API_KEY` is set, the
   environment is used *instead of* the stored login, never mixed with it field by field,
   so a key can't end up sent to another org's server. Set the URL, key and organization
   together. A profile you pick explicitly (`--profile`, `strobes_use_profile`, or a
   just-finished sign-in) always uses the stored login.

| Variable | Meaning |
|---|---|
| `STROBES_URL` (or `STROBES_HOST`) | Tenant URL, e.g. `https://acme.strobes.co` |
| `STROBES_API_KEY` (or `STROBES_API_TOKEN`) | A MasterKey from *Settings → API access* |
| `STROBES_ORGANIZATION_ID` | Organization UUID |
| `STROBES_PROFILE` | Which stored profile to use (multi-org) |
| `STROBES_WORKSPACE_ID` | Default workspace for tools that take `workspace_id` |
| `STROBES_VERIFY_TLS=false` | Self-signed / dev servers |
| `STROBES_BASE_URL` + `STROBES_TENANT_HOST` | Dial one URL but send another `Host` (local dev) |

**Multiple organizations:** sign in to each one with `login --profile eu --url …`. Then
switch with `strobes-agents-mcp use eu` (this also changes the CLI's default), or use the
`strobes_use_profile` tool for a single session. To pin one client to a profile, use
`install … --profile eu`.

Other commands: `strobes-agents-mcp status` checks who you are and tests the connection,
`profiles` lists your profiles, and `logout` signs you out.

---

## Tools

| Group | Tools |
|---|---|
| Session | `strobes_whoami`, `strobes_login`, `strobes_login_status`, `strobes_use_profile`, `strobes_logout` |
| Agents & workspaces | `list_agents`, `list_workspaces`, `get_workspace`, `create_workspace`, `activate_workspace`, `archive_workspace` |
| Tasks (agent work) | `create_task` (spin up, optionally wait), `wait_for_task`, `get_task`, `get_task_messages`, `list_tasks`, `cancel_task`, `retry_task` |
| Outputs | `list_findings`, `list_assets`, `list_files`, `read_file`, `get_download_url`, `upload_file` |
| Oversight | `list_approvals`, `get_credit_usage` |
| Workflows | `list_workflow_templates`, `start_workflow`, `list_workflows`, `get_workflow`, `wait_for_workflow` (live monitor), `control_workflow` (pause / resume / cancel / restart / restart a phase / advance a manual gate) |
| Workflow authoring | `workflow_authoring_catalog`, `export_workflow_template`, `validate_workflow_spec`, `save_workflow_template`, `attach_workflow`, `edit_workflow`, `set_phase_loop`, `detach_workflow`, `delete_workflow_template` |

Prompts (show up as slash commands / prompt pickers in most clients):

| Prompt | What it does |
|---|---|
| `run_agent(target, goal)` | Pick an agent, run a task on a target, report back |
| `run_workflow(target, template)` | Pick a template, launch it, follow it phase by phase |
| `design_workflow(goal, base_template)` | Write a custom workflow, validate it, save it as a template, optionally run it |
| `monitor_workflow(workspace_id)` | Live-follow a running workflow and flag what needs you |
| `agent_status(workspace_id)` | Status board of running/queued work and pending approvals |
| `triage_findings(workspace_id, min_severity)` | Review, de-duplicate and rank findings |
| `investigate_task(task_id, workspace_id)` | Root-cause a failed or stalled task |
| `collect_reports(workspace_id)` | Find, read and link the deliverables |
| `summarize_workspace(workspace_id)` | Executive brief on a workspace |
| `credit_report()` | Where AI credits are going |

How the tools are designed:

- **Annotations.** Every tool declares MCP annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`), so hosts can auto-approve reads and ask before anything destructive.
- **Default workspace.** `workspace_id` defaults to the workspace you picked at sign-in.
- **Waiting.** `create_task(wait_seconds=…)` and `wait_for_task` stream **progress
  notifications** while the agent works, then return the agent's final answer.
  `wait_for_workflow` does the same for workflows: it returns a timeline of phase and
  task changes, and comes back early when a person is needed (input request, manual
  gate, paused, blocked).
- **Writing workflows.** A spec is the same portable shape the web app exports as YAML
  (`{name, required_variables, phases: [{key, name, tasks: [{title, instructions,
  agent_type}]}]}`). Built-in templates can be hundreds of KB, so
  `export_workflow_template(save_to="workflows/x.yaml")` writes the file and returns only an
  outline. You edit the file, then pass it back as `spec_path` to `validate_workflow_spec`,
  `save_workflow_template` (an org-wide `custom:` template) or `attach_workflow` (run it in a
  workspace). Every write is validated on the server first, and an invalid spec is refused
  with its errors. Spec files must be under the project folder (or `STROBES_MCP_UPLOAD_ROOTS`).
- **Compact transcripts.** Agent transcripts drop the model's reasoning blocks by default.
  A single message can be 100 KB, mostly reasoning, and you can still see it with
  `include_thinking=true`.
- **Errors.** Failures are returned as MCP tool errors with a next step, for example
  *"Strobes rejected the API key… call `strobes_login`"*.
- **No duplicate side effects.** Mutations are never retried automatically. Reads retry
  transient 502/503/504 errors.
- **No credential uploads.** `upload_file(local_path=…)` refuses hidden files, keys and
  the Strobes credential store. Agent output comes from systems under test, so a prompt
  injection shouldn't be able to exfiltrate secrets through it.

---

## Remote / shared deployment (streamable HTTP)

```bash
strobes-agents-mcp serve --http --host 0.0.0.0 --port 8765 \
  --require-header-auth --allowed-host mcp.internal.example
```
Each client sends its own credentials:
```json
{
  "mcpServers": {
    "strobes-agents": {
      "type": "http",
      "url": "https://mcp.internal.example/mcp",
      "headers": {
        "Authorization": "Bearer <Strobes MasterKey>",
        "X-Strobes-Url": "https://acme.strobes.co",
        "X-Strobes-Organization-Id": "<org uuid>"
      }
    }
  }
}
```

What remote mode guarantees:

- **Header credentials only.** With `--require-header-auth`, the server never falls back
  to a login stored on the host.
- **No host-affecting tools.** Sign-in, sign-out, profile switching and `upload_file(local_path=…)`
  are refused to HTTP clients. `strobes_whoami` never lists the host's profiles.
- **Allowed URLs only.** `X-Strobes-Url` must be `https` and match
  `STROBES_MCP_ALLOWED_URLS` (default `*.strobes.co`; comma-separated globs), so callers
  can't point the server at internal addresses.
- **Host-header checks.** `--allowed-host` turns on Host-header (DNS-rebinding) checks for
  your public name. Without it, a non-localhost bind turns them off so requests through a
  load balancer aren't rejected.

For a single-tenant deployment, leave out `--require-header-auth` and `X-Strobes-Url`, and
set `STROBES_URL` / `STROBES_ORGANIZATION_ID` on the server. Clients then only send their
key. A container image is in [`Dockerfile`](Dockerfile).

## How it talks to Strobes

The server uses the Strobes **public GraphQL API** (`/api/public/graphql/`) and the agents
REST API (`/api/v1/organizations/<org>/cli/…`), both authenticated with a MasterKey. It
depends only on `mcp` and `httpx`.

- **Where agents run.** Agent work always runs in Strobes' cloud. Tasks are created over
  GraphQL, so they never use the CLI's "run tools on my machine" mode.
- **Workspace activation.** A new workspace normally starts in `initializing` and waits
  for a setup chat. `create_workspace` and `start_workflow` skip that setup by default so
  agents can start right away. On servers that expose the `skipWorkspaceSetup` mutation,
  the server calls it. On older ones, it falls back to an equivalent REST call.

## Development

```bash
uv sync
uv run pytest                                   # unit tests (mocked HTTP, in-memory MCP session)
STROBES_LIVE=1 uv run pytest tests/test_live.py  # read-only checks against your tenant
STROBES_LIVE=1 STROBES_LIVE_WRITE=1 uv run pytest tests/test_live.py   # + spins up a tiny agent task
uv run strobes-agents-mcp serve                 # stdio; try it with `npx @modelcontextprotocol/inspector`
```
Debug logging goes to stderr: `STROBES_LOG_LEVEL=INFO`.

When you open this repo in Claude Code, the project `.mcp.json` runs the server from your
checkout. Approve it when prompted. The plugin's own config is
`.claude-plugin/mcp.json`, and it uses `${CLAUDE_PLUGIN_ROOT}`.

<!-- mcp-name: io.github.strobes-co/strobes-agents-mcp -->
