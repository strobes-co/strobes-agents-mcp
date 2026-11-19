---
description: What are my Strobes agents doing right now?
argument-hint: "[workspace name or id]"
---
Give me a status board for Strobes agent work ($ARGUMENTS, else the default workspace).
Use `get_workspace`, `list_tasks` (show running/queued first, then recent completed/failed with
their errors), `get_workflow` if there is one, and `list_approvals` (pending items need me in
the web app). Keep it to a compact table plus one line of suggested next steps.
