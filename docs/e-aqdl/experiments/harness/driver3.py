"""比較実験のドライバー: 1つの条件を Claude Code（ヘッドレス）で動かし、replies.md の規則で返答する。

使い方: python3 driver.py <条件名> <作業フォルダ> <AQDLC_HOME> <プロンプトファイル> [--plugin] [--scope none|all]
        [--extra "<追記する1文>"] [--memory-end "<返答>"] [--pause-on-share]
"""
import json
import os
import re
import subprocess
import sys
import time

PLUGIN = "/home/claude/aq-dlc/plugins/aq-dlc"
ENGINE = PLUGIN + "/scripts/aqdlc.py"
MAX_REPLIES = 10


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1]
    return default


cond, work, home, prompt_file = sys.argv[1:5]
plugin = "--plugin" in sys.argv
scope = arg("--scope")
extra = arg("--extra")
mem_end = arg("--memory-end", "プロジェクト内だけに保存してください")
pause_share = "--pause-on-share" in sys.argv
resume = "--resume" in sys.argv
reply_arg = arg("--reply")
log_dir = os.path.join(os.path.dirname(work), "logs")
os.makedirs(log_dir, exist_ok=True)
env = dict(os.environ, AQDLC_HOME=home)
summary_path = os.path.join(log_dir, "summary.json")
summary = json.load(open(summary_path)) if resume and os.path.exists(summary_path) else \
    {"cond": cond, "turns": [], "replies": []}


def state():
    try:
        cur = open(os.path.join(work, ".aq-dlc", "runs", "CURRENT")).read().strip()
        return json.load(open(os.path.join(work, ".aq-dlc", "runs", cur, "state.json")))
    except (OSError, ValueError):
        return None


def run_claude(text, cont):
    n = len(summary["turns"]) + 1
    cmd = ["claude", "-p", text, "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
           "--max-turns", "60"]
    if cont:
        cmd.insert(1, "-c")
    if plugin:
        cmd += ["--plugin-dir", PLUGIN]
    t0 = time.time()
    out = open(os.path.join(log_dir, f"turn{n:02d}.jsonl"), "w")
    subprocess.run(cmd, cwd=work, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=3600)
    out.close()
    res = {}
    for ln in open(os.path.join(log_dir, f"turn{n:02d}.jsonl"), encoding="utf-8", errors="replace"):
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("type") == "result":
            res = o
    rec = {"n": n, "input": text, "sec": round(time.time() - t0, 1), "num_turns": res.get("num_turns"),
           "cost_usd": res.get("total_cost_usd"), "usage": res.get("usage"), "result": (res.get("result") or "")[-3000:]}
    summary["turns"].append(rec)
    json.dump(summary, open(summary_path, "w"), ensure_ascii=False, indent=2)
    return rec["result"]


SIM_PROMPT = open("/root/.exp-hidden/simulator_prompt.md", encoding="utf-8").read()
INTENT = open(arg("--intent", "/root/.exp-hidden/intent03.md"), encoding="utf-8").read()


def simulate(text, doc=""):
    p = SIM_PROMPT.replace("{INTENT}", INTENT).replace("{DOC}", doc or "（なし）").replace("{MESSAGE}", text[-6000:])
    simdir = os.path.join(os.path.dirname(work), "sim")
    os.makedirs(simdir, exist_ok=True)
    r = subprocess.run(["claude", "-p", p, "--tools", "", "--output-format", "json"], cwd=simdir,
                       capture_output=True, text=True, timeout=600)
    o = json.loads(r.stdout)
    summary.setdefault("sim_cost", 0)
    summary["sim_cost"] += o.get("total_cost_usd") or 0
    return (o.get("result") or "").strip()


def decide(text):
    """機械的な返答（Memory・GitHub）以外は、意図を持った依頼者役が返す。None なら終了。"""
    s = state() if plugin else None
    if plugin and s and s.get("status") in ("complete", "closed", "pr_ready", "aborted"):
        return None
    if plugin and s and not s.get("memory_scope") and s.get("status") in ("in_progress", "pr_ready"):
        if scope == "all":
            m = re.search(r"MEMSCOPE-[0-9a-f]{8}", text)
            if not m:
                out = subprocess.run([sys.executable, ENGINE, "memory-scope", "all", "--project", work], env=env,
                                     capture_output=True, text=True).stdout + ""
                m = re.search(r"MEMSCOPE-[0-9a-f]{8}", out)
            return f"共通 Memory も使う（承認 {m.group(0)}）" if m else "共通 Memory も使う"
        return "Memory を使わない"
    if plugin and s and s.get("status") in ("in_progress", "pr_ready") and not s.get("memory_done") \
            and re.search(r"保存先|保存しない|Memory 整理", text):
        return mem_end
    doc = ""
    if plugin and s:
        sp = s.get("spec") or {}
        if sp.get("presented_hash") and sp.get("approved_hash") != sp.get("presented_hash"):
            try:
                cur = open(os.path.join(work, ".aq-dlc", "runs", "CURRENT")).read().strip()
                doc = open(os.path.join(work, ".aq-dlc", "runs", cur, "artifacts", "spec.md"), encoding="utf-8").read()[:20000]
                doc = f"（承認の ID: SPEC-{sp['presented_hash'][:8]}）\n" + doc
            except OSError:
                pass
    r = simulate(text, doc)
    if r.strip() == "END" or not r.strip():
        if plugin and s and s.get("status") == "in_progress":
            return "続けてください"
        return None
    return r


def main():
    if reply_arg:
        text = run_claude(reply_arg, True)
        summary["replies"].append(reply_arg)
    elif not resume:
        p = open(prompt_file, encoding="utf-8").read()
        if extra:
            p = p.rstrip() + "\n\n" + extra + "\n"
        text = run_claude(p, False)
    else:
        text = summary["turns"][-1]["result"]
    while len(summary["replies"]) < MAX_REPLIES:
        r = decide(text)
        if r is None:
            break
        if r == "__PAUSE__":
            print("PAUSE: 共有候補の確認待ち")
            json.dump(summary, open(summary_path, "w"), ensure_ascii=False, indent=2)
            return
        summary["replies"].append(r)
        text = run_claude(r, True)
    summary["finished"] = True
    json.dump(summary, open(summary_path, "w"), ensure_ascii=False, indent=2)
    print("DONE", len(summary["turns"]), "turns,", len(summary["replies"]), "replies")


main()
