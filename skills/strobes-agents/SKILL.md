---
name: strobes-agents
description: Drive Strobes AI agents through the strobes-agents MCP server — sign in, spin up or stop agent tasks (pentest, recon, triage, cloud, reporting …) and follow them to a result, read what agents produced (findings, assets, files), upload scope docs, and run or control multi-phase workflows. Use when the user says things like "have a Strobes agent test X", "spin up a pentest agent", "run the web app pentest workflow on X", "what are my agents doing", "stop that task", "show the workspace findings", "pull the report", "upload this scope doc", or "sign in to Strobes".
---

# Strobes AI agents

Tools come from the `strobes-agents` MCP server. Everything runs in Strobes' cloud;
this side only starts work and reads results.

## Sign-in (no API key to paste)
1. `strobes_whoami` — if `signed_in` is false:
2. `strobes_login(url?)` → show the user `verification_url` (clickable) and `user_code`;
   they confirm the code, pick an organization (and optionally a default workspace) and
   click Authorize.
3. `strobes_login_status(wait_seconds=120)` until `approved`.
The login is shared with the Strobes CLI (`strobes login`); `strobes_use_profile`
switches between signed-in organizations.

## Concepts
- **Workspace** — container for agent work: tasks, files, findings, assets. Must be
  `active` to take tasks (`create_workspace` activates by default; `activate_workspace`
  fixes an `initializing` one).
- **Agent** — e.g. `web_pentest_agent`, `api_pentest_agent`, `aws_pentest_agent`,
  `vuln_triage_agent`, `report_writer_agent`, `general_security_assistant`. Get the real
  list from `list_agents`.
- **Task** — one unit of agent work, created with natural-language instructions. It is
  the only way to "talk" to an agent; its conversation is `get_task_messages`.
- **Workflow** — a multi-phase pipeline of tasks from a template (`start_workflow`).

`workspace_id` is optional everywhere: it defaults to the workspace picked at sign-in.

## Flows
**Get an agent to do something**
1. `list_agents(search=...)` → agent id. Reuse a workspace (`list_workspaces(status="active")`)
   or `create_workspace(name)`. Upload context first if useful: `upload_file(path="docs/scope.md", content=...)`.
2. `create_task(instructions, agent, workspace_id)` — write instructions like a brief to a
   pentester: target(s), scope boundaries, credentials to use, what to deliver.
3. `wait_for_task(task_id)` — blocks with progress up to `timeout_seconds`; call again
   while `status` is still running. The response carries the agent's `result`.
4. Evidence: `list_findings` (filter `severity=["critical","high"]`), `list_files` →
   `read_file`, `get_download_url` for big/binary files or a zip.
5. `cancel_task` to stop, `retry_task` to re-run a failed one.

**Run a workflow**
`list_workflow_templates` → `start_workflow(template_slug, variables={"target": ...})` →
`wait_for_workflow(workspace_id, until="phase_change")` in a loop to follow it live (one-line
update per phase; it returns early with `needs_attention` when a person is needed) →
`get_workflow` for a full snapshot → `control_workflow(action=pause|resume|cancel|restart|restart_phase|advance)`.

**Status board** — `get_workspace` (stats), `list_tasks` (`by_status` counts),
`get_workflow`, `list_approvals` (pending human approvals happen in the web app),
`get_credit_usage`.

## Guidance
- Tasks are asynchronous and cost AI credits — never create duplicates "to check"; use
  `get_task` / `wait_for_task`. Don't blindly retry budget/credit failures; tell the user.
- `get_task_messages` hides agent reasoning by default; pass `include_thinking=true` only
  when the user wants it. Page older history with `offset=older_offset`.
- Stay inside the scope the user gave. Confirm before archiving workspaces, cancelling
  workflows, or launching work against targets the user hasn't named.
- Errors come back as tool errors with a next step (e.g. sign in again) — follow it.
