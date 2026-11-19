---
description: Give a Strobes AI agent a task and follow it to a result
argument-hint: "<what the agent should do>"
---
Use the Strobes agents MCP tools to get this done by a Strobes AI agent: $ARGUMENTS

- `strobes_whoami` first (run the /strobes-agents-mcp:login flow if not signed in).
- Pick the best agent from `list_agents`; use the default workspace, or the most relevant
  `list_workspaces(status="active")` one, or `create_workspace` if none fits.
- `create_task` with precise instructions (targets, scope limits, deliverables), then
  `wait_for_task` until it finishes, calling it again while it's still running.
- Report the result, then any `list_findings` and new `list_files` outputs.
Ask me before anything destructive or outside the stated scope.
