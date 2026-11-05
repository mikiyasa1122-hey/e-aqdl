import glob, json, os, subprocess, sys
B = "/tmp/claude-0/exp14"; S = os.path.dirname(os.path.abspath(__file__))
BASE = subprocess.run(["git", "-C", f"{B}/fixture", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
rows = []
for cond in ("before", "after"):
    for i in (1, 2, 3):
        d = f"{B}/{cond}-{i}"
        s = json.load(open(f"{d}/logs/summary.json"))
        ev = json.loads(subprocess.run(["python3", "-I", f"{S}/evaluate14.py", f"{d}/app", BASE], capture_output=True, text=True).stdout)
        st = json.load(open(sorted(glob.glob(f"{d}/app/.aq-dlc/runs/*/state.json"))[-1]))
        cfg = json.load(open(f"{d}/app/.aq-dlc/config.json"))
        m = subprocess.run(["python3", f"{B}/engines/{cond}/plugins/aq-dlc/scripts/aqdlc.py", "metrics", "--json", "--project", f"{d}/app"],
                           capture_output=True, text=True, env=dict(os.environ, AQDLC_HOME=f"{d}/home"))
        try:
            mj = json.loads(m.stdout)
        except ValueError:
            mj = {}
        roles = mj.get("roles") or {}
        def tok(r):
            t = (roles.get(r) or {}).get("tokens") or {}
            return sum((t.get(k) or 0) for k in ("input", "cache_read", "cache_creation", "output")) if t else None
        rw = mj.get("rework") or {}
        lint = [c["command"] for c in cfg.get("checks", []) if c.get("kind") == "lint" or "ruff" in c["command"]]
        rows.append({
            "run": f"{cond}-{i}", "replies": len(s["replies"]), "cost": round(sum(t["cost_usd"] or 0 for t in s["turns"]), 2),
            "sec": round(sum(t["sec"] for t in s["turns"])), "status": st.get("status"),
            "lint_cmd": lint, "diff_cov_cfg": bool(((cfg.get("quality") or {}).get("diff_coverage") or {}).get("enabled")),
            "gate_runs": rw.get("gate_runs"), "gate_fails": rw.get("gate_failures"), "review_rounds": rw.get("review_rounds"),
            "reviewer_tokens": tok("reviewer"), "implementer_tokens": tok("implementer"),
            "pre_existing_modified": ev["pre_existing_modified"], "lint_changed": ev["lint_changed"],
            "report_py_lint": ev["report_py_lint"], "diff_cov": ev["diff_cov"]["percent"], "uncovered": ev["diff_cov"]["uncovered"],
            "hidden_failed": ev["hidden"]["failed"], "hidden_ran": ev["hidden"]["ran"], "visible_ok": ev["visible_tests_ok"],
            "test_live_unchanged": ev["test_live_unchanged"], "rework": rw,
        })
json.dump(rows, open(f"{B}/results14.json", "w"), ensure_ascii=False, indent=1)
for r in rows:
    print(json.dumps({k: v for k, v in r.items() if k != "rework"}, ensure_ascii=False))
