"""End-to-end test for strobes-agents-mcp against a live Strobes backend."""
import os, sys, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strobes_agents_mcp import server  # noqa: E402

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}"[:200])

def main():
    r = server.strobes_agents_whoami(); check("whoami", r.get("connected") is True, json.dumps(r)[:120])
    r = server.list_agents(); check("list_agents", r.get("count", 0) > 0, f"n={r.get('count')}")
    agent_id = (r.get("agents") or [{}])[0].get("id")

    r = server.list_workspaces(limit=5); check("list_workspaces", "workspaces" in r, f"n={r.get('count')}")
    active = next((w for w in (r.get("workspaces") or []) if w.get("status") == "ACTIVE"), None)
    wid = (active or {}).get("id"); check("active_workspace", bool(wid), f"id={wid}")

    if wid:
        check("get_workspace", server.get_workspace(wid).get("workspace", {}).get("id") == wid)
        check("workspace_stats", "stats" in server.workspace_stats(wid))
        check("list_workspace_tasks", "tasks" in server.list_workspace_tasks(wid))
        check("list_workspace_findings", "findings" in server.list_workspace_findings(wid))
        check("list_workspace_assets", "assets" in server.list_workspace_assets(wid))
        files = server.list_workspace_files(wid); check("list_workspace_files", "files" in files, f"n={files.get('count')}")
        check("download_workspace", bool(server.download_workspace(wid).get("download_url")))
        if files.get("files"):
            fp = files["files"][0]["path"]
            gf = server.get_workspace_file(wid, fp)
            check("get_workspace_file", bool(gf.get("download_url")), f"inline={'content' in gf}")

        # tasks: spin up + stop
        t = server.create_workspace_task(wid, "e2e task", "say hi",
                                         agent_type=agent_id or "general_security_assistant")
        tid = (t.get("task") or {}).get("id")
        check("create_workspace_task(spin up)", bool(tid), f"err={t.get('error')}")
        if tid:
            check("cancel_workspace_task(stop)", server.cancel_workspace_task(wid, tid).get("ok") is True)
            tm = server.get_task_messages(wid, tid)
            check("get_task_messages", "messages" in tm, f"n={tm.get('count')}")

        # workflow (this ws has none → graceful)
        check("get_workspace_workflow", "workflow" in server.get_workspace_workflow(wid))

    # workflow templates + control reachability
    tpl = server.list_workflow_templates(); check("list_workflow_templates", tpl.get("count", 0) > 0, f"n={tpl.get('count')}")
    if wid:
        pw = server.pause_workflow(wid)
        # no workflow on this ws → structured error is the expected, correct outcome
        check("pause_workflow(reachable)", ("error" in pw) or (pw.get("ok") is not None),
              "no-workflow error OK" if "error" in pw else "ok")

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILURES:", FAIL); sys.exit(1)

if __name__ == "__main__":
    main()
