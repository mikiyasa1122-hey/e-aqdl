"""PERF-01 の試行（パイロット）: 通常の Claude Code と E-AQDL の全工程を、同じ課題で比べる。

  python3 bench/workflow/run_workflow.py run --cond plain|eaqdl --rep N --out DIR [--max-turns 10] [--budget 30]
  python3 bench/workflow/run_workflow.py evaluate --out DIR
  python3 bench/workflow/run_workflow.py report --out DIR

課題: starter/（db.py・errors.py・仕様書・インターフェースの契約）から、notes パッケージを実装する。
- 両方の条件に、同じ初期のリポジトリと同じ依頼の文を渡す。E-AQDL だけ、プラグイン（--plugin-dir）を読み込み、依頼の先頭に
  /aq-dlc:start を付ける。
- 人の役（台本）: 実行が止まったら、決まった文で返答する（resume）。仕様の承認（SPEC-… を承認）、Memory はプロジェクト内だけ、
  推奨の選択で進める、PR と push はしない、の4点。返答の回数を「人への確認」として数える。台本は両方の条件で同じ。
- 完成の評価: 実装担当に見せていない隠しテスト（bench/review/app_clean/tests の 63件と、30件の欠陥・残っていた欠陥 5件・
  SQL の追加 3件の repro）を、出来上がったコードに対して実行する。
- 記録: 各ターンの費用（total_cost_usd。サブエージェントを含む）・時間・ターン数・使ったモデル・最後の状態。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
REVIEW = os.path.join(ROOT, "bench", "review")
sys.path.insert(0, REVIEW)
PLUGIN = os.path.join(ROOT, "plugins", "aq-dlc")

TASK = """docs/spec.md の仕様と docs/contract.md のインターフェースの契約に従って、notes パッケージ（notes/service.py・
notes/ratelimit.py・notes/share.py）と、そのテスト（tests/ に unittest。`python3 -m unittest discover -s tests -t .` で実行）を作ってください。
notes/db.py と notes/errors.py は用意済みで、変えないでください。Python 3 の標準ライブラリだけを使います。
進め方: Memory を使う場合はプロジェクト内だけにしてください。選択肢のある質問は、推奨の選択で進めてください。
PR の作成とリモートへの push は不要です（ローカルで完了まで進めてください）。"""

# 人の役の返答（台本）。仕様の承認を求められたら、承認だけを1行で返す。E-AQDL は、承認と他の指示・否定の表現が混ざった返答を
# 承認として扱わない（最初の試行で、混ぜた台本が受理されないことを確かめた。results/pilot/ANALYSIS.md）
HUMAN_APPROVE = "承認 {ids}"
HUMAN_REPLY = "推奨の選択で進めてください。"


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)


def setup_project(dest):
    shutil.copytree(os.path.join(HERE, "starter"), dest)
    with open(os.path.join(dest, "README.md"), "w", encoding="utf-8") as f:
        f.write("# notes\n組織ごとのメモのライブラリ。仕様は docs/spec.md、契約は docs/contract.md。\n")
    git(dest, "init", "-q", "-b", "main")
    git(dest, "-c", "user.name=bench", "-c", "user.email=bench@local", "add", "-A")
    git(dest, "-c", "user.name=bench", "-c", "user.email=bench@local", "commit", "-q", "-m", "初期状態")


def claude(cwd, prompt, cond, session=None, budget=None, env=None):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
    if cond == "eaqdl":
        cmd += ["--plugin-dir", PLUGIN]
    if session:
        cmd += ["--resume", session]
    if budget:
        cmd += ["--max-budget-usd", str(budget)]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=4 * 3600)
    try:
        res = json.loads(p.stdout)
    except ValueError:
        res = {"is_error": True, "result": (p.stdout + p.stderr)[-3000:]}
    return {"sec": round(time.time() - t0, 1), "exit": p.returncode, "cost_usd": res.get("total_cost_usd"),
            "num_turns": res.get("num_turns"), "session_id": res.get("session_id"),
            "models": sorted((res.get("modelUsage") or {}).keys()), "is_error": bool(res.get("is_error")),
            "result": res.get("result"), "usage": res.get("usage")}


def eaqdl_state(project):
    """E-AQDL の run の状態（なければ None）。"""
    runs = os.path.join(project, ".aq-dlc", "runs")
    if not os.path.isdir(runs):
        return None
    latest = sorted(d for d in os.listdir(runs) if os.path.isfile(os.path.join(runs, d, "state.json")))
    if not latest:
        return None
    try:
        with open(os.path.join(runs, latest[-1], "state.json"), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    return {"status": s.get("status"), "reviews": len(s.get("reviews", [])), "fix_iterations": s.get("fix_iterations", 0),
            "impl_rounds": len((s.get("impl") or {}).get("rounds", [])),
            "human_events": len(s.get("human_events", []) or []), "spec_version": (s.get("spec") or {}).get("version"),
            "spec_approved": bool(s.get("approvals")), "asks_total": s.get("asks_total"),
            "stop_blocks_total": s.get("stop_blocks_total")}


def finished(cond, project, last):
    if last["is_error"]:
        return True
    if cond == "eaqdl":
        st = eaqdl_state(project)
        return bool(st and st["status"] in ("complete", "pr_ready", "closed", "aborted"))
    # 通常の Claude Code: 質問で止まっていなければ終わり
    text = last.get("result") or ""
    return not re.search(r"[?？]\s*$|どちら|よろしいですか|確認させて|教えてください", text.strip()[-300:])


def run(cond, rep, out, max_turns, budget):
    tag = f"{cond}-{rep}"
    dest = os.path.join(out, "runs", tag + ".json")
    if os.path.exists(dest):
        print(tag, "skip")
        return
    project = os.path.join(out, "work", tag)
    shutil.rmtree(project, ignore_errors=True)
    setup_project(project)
    home = os.path.join(out, "home", tag)  # E-AQDL の全体の Memory を、試行ごとに分ける
    os.makedirs(home, exist_ok=True)
    env = dict({k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}, AQDLC_HOME=home)
    turns, session, spent, prev_cum = [], None, 0.0, 0.0
    prompt = ("/aq-dlc:start " if cond == "eaqdl" else "") + TASK
    for i in range(max_turns):
        left = max(budget - spent, 0.5)
        t = claude(project, prompt, cond, session, left, env)
        # total_cost_usd は、resume したセッションの累計。この回の分は差で出す
        cum = t["cost_usd"] or 0
        t["cost_cumulative_usd"], t["cost_usd"] = cum, round(max(cum - prev_cum, 0), 4) if cum >= prev_cum else cum
        prev_cum = max(cum, prev_cum)
        t["prompt"] = prompt
        t["state_after"] = eaqdl_state(project)
        turns.append(t)
        spent += t["cost_usd"] or 0
        session = t["session_id"] or session
        print(tag, f"turn {i + 1}", f"${t['cost_usd']}", f"{t['sec']}s", t["state_after"], flush=True)
        if finished(cond, project, t) or spent >= budget:
            break
        ids = sorted(set(re.findall(r"SPEC-[0-9A-Za-z-]+", t.get("result") or "")))
        st = t["state_after"] or {}
        prompt = HUMAN_APPROVE.format(ids=" ".join(ids)) if ids and not st.get("spec_approved") else HUMAN_REPLY
    rec = {"cond": cond, "rep": rep, "turns": turns, "human_replies": len(turns) - 1,
           "cost_usd": round(spent, 4), "sec": round(sum(t["sec"] for t in turns), 1),
           "final_state": eaqdl_state(project), "budget_hit": spent >= budget,
           "stopped_by": "max_turns" if len(turns) == max_turns and not finished(cond, project, turns[-1]) else "done"}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ evaluate
def hidden_suite(project):
    """隠しテストを、出来上がったコードのコピーに対して実行する（作業フォルダは変えない）。"""
    import cases as CS
    import clean_cases as CL
    import sql_cases as SQ
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "p")
        shutil.copytree(project, work, ignore=shutil.ignore_patterns(".git", ".aq-dlc", "__pycache__"))
        hid = os.path.join(work, "hidden_tests")
        shutil.copytree(os.path.join(REVIEW, "app_clean", "tests"), hid)
        repros = [c for c in CS.CASES + SQ.CASES if c["defect"]] + [{"id": k, "repro": v} for k, v in CL.RESIDUAL_REPROS.items()]
        # SEC-03 の repro は assertLogs を使い、ログが1行も出ないと失敗する。契約ではログは任意なので、
        # ログを出さない実装を誤って不合格にしないよう、ここではログを集めて確かめる形に置き換える
        sec03 = ("    def test(self):\n        import logging\n        out = []\n"
                 "        h = logging.Handler()\n        h.emit = lambda r: out.append(r.getMessage())\n"
                 "        lg = logging.getLogger('notes.share')\n        lg.addHandler(h)\n        lg.setLevel(logging.DEBUG)\n"
                 "        link = share.make_link('s3cret', 7, ttl_sec=60, now=1000)\n        lg.removeHandler(h)\n"
                 "        self.assertNotIn(link.split('.')[2], '\\n'.join(out))\n")
        repros = [dict(c, repro=sec03) if c["id"] == "SEC-03" else c for c in repros]
        for c in repros:
            with open(os.path.join(hid, f"test_repro_{c['id'].replace('-', '_').lower()}.py"), "w", encoding="utf-8") as f:
                f.write(CS.REPRO_HEAD + c["repro"])
        runner = ("import json,sys,unittest\n"
                  "s=unittest.defaultTestLoader.discover('hidden_tests',top_level_dir='.')\n"
                  "r=unittest.TestResult();s.run(r)\n"
                  "print(json.dumps({'run':r.testsRun,'failures':[t.id() for t,_ in r.failures],"
                  "'errors':[t.id() for t,_ in r.errors]}))\n")
        p = subprocess.run([sys.executable, "-c", runner], cwd=work, capture_output=True, text=True, timeout=600)
        try:
            res = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": (p.stdout + p.stderr)[-2000:]}
        own = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=work,
                             capture_output=True, text=True, timeout=600)
        m = re.search(r"Ran (\d+) test", own.stderr)
        bad = set(res["failures"]) | set(res["errors"])
        repro_ids = [t for t in bad if ".test_repro_" in t]
        return {"hidden_run": res["run"], "hidden_failed": len(bad), "hidden_pass_rate": 1 - len(bad) / res["run"],
                "repro_failed": sorted({t.split(".test_repro_")[1].split(".")[0] for t in repro_ids}),
                "failed": sorted(bad), "own_tests": int(m.group(1)) if m else 0, "own_tests_ok": own.returncode == 0}


def evaluate(out):
    for fn in sorted(os.listdir(os.path.join(out, "runs"))):
        p = os.path.join(out, "runs", fn)
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
        rec["evaluation"] = hidden_suite(os.path.join(out, "work", fn[:-5]))
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        print(fn, {k: v for k, v in rec["evaluation"].items() if k != "failed"})


def report(out):
    rows = []
    for fn in sorted(os.listdir(os.path.join(out, "runs"))):
        with open(os.path.join(out, "runs", fn), encoding="utf-8") as f:
            r = json.load(f)
        ev = r.get("evaluation") or {}
        fs = r.get("final_state") or {}
        rows.append(f"| {r['cond']} | {r['rep']} | {ev.get('hidden_pass_rate', 'not_measured'):.3f} "
                    f"({ev.get('hidden_run', 0) - ev.get('hidden_failed', 0)}/{ev.get('hidden_run', 0)}) | "
                    f"{', '.join(ev.get('repro_failed', [])) or '-'} | {ev.get('own_tests', '-')}"
                    f"{'' if ev.get('own_tests_ok') else '（失敗あり）'} | ${r['cost_usd']} | {r['sec'] / 60:.1f} 分 | "
                    f"{len(r['turns'])} | {r['human_replies']} | {fs.get('status', '-')} | {fs.get('reviews', '-')} | "
                    f"{fs.get('fix_iterations', '-')} | {r['stopped_by']}{'・予算の上限' if r['budget_hit'] else ''} |")
    md = ("# PERF-01 パイロットの結果\n\n| 条件 | 回 | 隠しテストの合格率 | 失敗した repro | 自作テスト | 費用 | 時間 | 実行の回数 | "
          "人の返答 | E-AQDL の状態 | レビュー | 修正の回数 | 終わり方 |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
          + "\n".join(rows) + "\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    cmd, out = sys.argv[1], os.path.abspath(arg("--out", os.path.join(HERE, "results", "pilot")))
    if cmd == "run":
        run(arg("--cond"), int(arg("--rep", "1")), out, int(arg("--max-turns", "10")), float(arg("--budget", "30")))
    elif cmd == "evaluate":
        evaluate(out)
    elif cmd == "report":
        report(out)
    else:
        sys.exit(__doc__)
