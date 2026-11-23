# Changelog

## 1.1.0
- **Live workflow monitoring**: `wait_for_workflow` polls a running workflow with progress
  notifications and returns a timeline of phase/task changes; it returns early when a
  person is needed (input request, manual gate, paused, blocked).
- `get_workflow` now lists each phase's tasks and a `needs_attention` list.
- `control_workflow` adds `restart`, `restart_phase` (optionally cascading) and `advance`
  (past a manual gate).
- New prompts: `run_workflow`, `monitor_workflow`, `agent_status`, `triage_findings`,
  `investigate_task`, `collect_reports`, `credit_report`.

## 0.2.0
- **Browser sign-in** (`strobes-agents-mcp login` / `strobes_login` tool), the same device flow
  as `strobes login`; credentials are shared with the Strobes CLI's profile store.
  Env vars still work and take precedence. Multi-org profiles (`use`, `strobes_use_profile`).
- **One-command setup** for Claude Code, Claude Desktop, Cursor, VS Code, Windsurf, Codex and
  Gemini CLI (`setup`, `install <client>`). The server starts without credentials, and the
  plugin config needs no env vars.
- **Follow agents to a result**: `wait_for_task` and `create_task(wait_seconds=…)` with progress
  notifications; `get_task` returns the agent's final answer; transcripts drop reasoning
  blocks by default (often 10–20× smaller).
- New tools: `activate_workspace`, `upload_file`, `read_file`, `get_download_url`,
  `list_approvals`, `get_credit_usage`, `control_workflow`; findings severity filter.
- `create_workspace` / `start_workflow` skip the interactive setup by default, so work starts
  right away (previously workspaces stayed `initializing` and couldn't take tasks).
- MCP best practices: tool annotations and titles on every tool, server instructions, prompts,
  real tool errors with next steps, typed/validated arguments.
- Streamable HTTP transport with per-request header auth (`serve --http --require-header-auth`)
  and a Dockerfile.
- Dropped the `strobes-graphql-client` git dependency (now `mcp` + `httpx` only), which makes
  the package installable with `uvx` / PyPI.
- Security hardening for shared/remote use: session tools are local-only, an allowlist
  for `X-Strobes-Url` (anti-SSRF), `--allowed-host`, and no host profile disclosure.
  Env credentials are never mixed with a stored profile, and `upload_file` refuses
  credential files.
- Config safety: a locked, strict read-modify-write means a corrupt `config.json` is never
  replaced. Writes to client configs are atomic and type-checked.
- Fixed:
  - workspace stats queried fields that don't exist on the server
  - concurrent HTTP requests closed each other's connections
  - `STROBES_INSECURE=0` disabled TLS verification
  - an expired sign-in stayed "pending", and `slow_down` was ignored
  - signing in to a new profile didn't switch to it
  - redirects were reported as success
  - large UTF-8 files were reported as binary
  - `install claude-code --profile` built a broken `claude mcp add` command
  - uvx cache paths weren't detected on Windows
