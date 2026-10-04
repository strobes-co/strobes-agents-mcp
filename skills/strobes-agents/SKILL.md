---
name: strobes-agents
description: Drive Strobes AI workspaces and agents via the Strobes Agents MCP server. Use when the user wants to manage AI workspaces, spin up or stop Strobes agent tasks, read a task's conversation/output, read what agents produced (findings/assets/files), or run and control workflows (start from template, pause/resume/cancel). Triggers on "spin up an agent", "run a pentest agent/workflow", "stop that task", "create a workspace", "show the task's messages", "show workspace findings/files", "pull the report file", "pause the workflow".
---

# Strobes AI workspaces & agents

Drives the `strobes-agents` MCP server, wrapping the Strobes **public** GraphQL
API (MasterKey auth). Companion skill `strobes` handles assets/findings/engagements.

## Concepts
- **Workspace** — container for agent work (tasks, files, findings, assets). Must
  be `ACTIVE` before you can create tasks in it.
- **Agent** — a system/custom AI agent (e.g. `web_pentest_agent`,
  `vuln_triage_agent`, `report_writer_agent`, `general_security_assistant`).
- **Task** — a unit of agent work. You spin it up with instructions + an agent;
  it runs async, holds a **conversation** (its messages) and produces output.
- **Workflow** — a multi-phase agent pipeline started from a template.

> Tasks are the single interaction model: there is no separate "chat". To talk
> to an agent, create a task; to read the exchange, use `get_task_messages`.

## First call
`strobes_agents_whoami` (connectivity), then `list_agents` for valid agent ids.

## Flows
**Spin up agent work & follow it**
1. `list_workspaces(status="active")` → a workspace id (or `create_workspace`).
2. `list_agents()` → an `agent_type` id.
3. `create_workspace_task(workspace_id, title, instructions, agent_type)` — queues it.
4. Poll `list_workspace_tasks` for status; `get_task_messages(workspace_id, task_id)`
   to read the agent's conversation/output (paginated, newest page first).
5. `cancel_workspace_task` to stop, `retry_workspace_task` to re-run.

**See what agents produced**
- `list_workspace_findings` / `list_workspace_assets`
- `list_workspace_files(workspace_id, path="", recursive=True)` to browse output
- `get_workspace_file(workspace_id, path)` to pull one file (inlines small text;
  always returns a `download_url`)
- `download_workspace(workspace_id)` for a zip URL of everything

**Run a workflow**
1. `list_workflow_templates()` → a `slug` and its `required_variables`.
2. `start_workflow(template_slug, variables, name=...)` — creates a workspace and
   kicks off the pipeline.
3. `get_workspace_workflow(workspace_id)` for status/phases; `pause_workflow` /
   `resume_workflow` / `cancel_workflow` to control it.

## Guidance
- Workspace/task ids are **UUIDs**; agent ids are **strings**.
- `create_workspace_task` returns immediately and runs **asynchronously** — poll
  `list_workspace_tasks` / `get_task_messages`, don't expect output in the same call.
- `get_task_messages` returns `[]` until the task's run has started.
- Agent runs consume AI credits; a cap returns a clear `{"error": ...}`
  (BUDGET_EXCEEDED) — surface it, don't retry blindly.
- Creating a task needs an **ACTIVE** workspace; `initializing` will error.
- Tools return `{"error": "..."}` on failure — read and adapt.
