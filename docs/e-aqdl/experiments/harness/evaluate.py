"""成果物の採点: 隠しテスト（観点別）と、各成果物自身のテストの欠陥検出能力（Mutation）。

使い方: python3 evaluate.py <条件名> <成果物のフォルダ>
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "/home/claude/aq-dlc/plugins/aq-dlc/scripts")
from aqdlc_core import mutation as MU  # noqa: E402

HIDDEN = "/root/.exp-hidden"
cond, app = sys.argv[1], sys.argv[2]
out = {"cond": cond}

# ---- 隠しテスト
p = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_acceptance"], cwd=HIDDEN,
                   env=dict(os.environ, SERVER_DIR=app), capture_output=True, text=True, timeout=900)
res = {}
for m in re.finditer(r"^(test_\w+) \(.*?\) \.\.\. (ok|FAIL|ERROR)", p.stderr, re.M):
    res[m.group(1)] = m.group(2) == "ok"
cats = {}
for name, ok in res.items():
    c = name.split("_")[1]
    a = cats.setdefault(c, [0, 0])
    a[1] += 1
    a[0] += ok
out["hidden"] = {"passed": sum(res.values()), "total": len(res), "by_category": cats,
                 "failed": sorted(n for n, ok in res.items() if not ok)}


# ---- 成果物自身のテスト
def find_tests(root):
    files = []
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if not x.startswith(".") and x not in ("__pycache__", "node_modules")]
        files += [os.path.join(d, f) for f in fs if re.match(r"test_.*\.py$|.*_test\.py$", f)]
    return files


def run_own_tests(root, timeout=300):
    """戻り値: (exit, 実行したテスト数, 出力)"""
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test*.py", "-t", "."],
                       cwd=root, capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    m = re.search(r"Ran (\d+) test", p.stderr)
    return p.returncode, int(m.group(1)) if m else 0, p.stderr[-2000:]


tests = find_tests(app)
out["own_tests"] = {"files": [os.path.relpath(f, app) for f in tests]}
if tests:
    code, n, _o = run_own_tests(app)
    out["own_tests"].update(exit=code, ran=n)
    # ---- Mutation（server.py に、同じ Operator を同じ規則で入れる）
    src = os.path.join(app, "server.py")
    lines = open(src, encoding="utf-8").read().splitlines(keepends=True)
    sites = MU.sites(lines, "python")
    killed, survived, invalid, details = 0, 0, 0, []
    for i, op in sites:
        new = MU.apply(op, lines, i, "python")
        text = "".join(lines[:i] + [new] + lines[i + 1:])
        ok, _why = MU.syntax_ok(text, "python")
        if not ok:
            invalid += 1
            continue
        tmp = tempfile.mkdtemp()
        try:
            dst = os.path.join(tmp, "app")
            shutil.copytree(app, dst, ignore=shutil.ignore_patterns(".git", ".aq-dlc", "__pycache__"))
            open(os.path.join(dst, "server.py"), "w", encoding="utf-8").write(text)
            try:
                c, n2, o = run_own_tests(dst, 120)
            except subprocess.TimeoutExpired:
                c, n2, o = 124, 0, "timeout"
            if c == 0:
                survived += 1
                details.append({"line": i + 1, "op": op, "result": "survived", "code": lines[i].strip()[:80]})
            elif re.search(r"SyntaxError|ImportError|ModuleNotFound", o) or n2 == 0:
                invalid += 1
            else:
                killed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    out["mutation"] = {"sites": len(sites), "killed": killed, "survived": survived, "invalid": invalid,
                       "score": round(killed / (killed + survived), 3) if killed + survived else None,
                       "survived_examples": details[:15]}
else:
    out["own_tests"].update(exit=None, ran=0)
    out["mutation"] = {"score": None, "note": "テストがないため測れない"}

json.dump(out, open(os.path.join(os.path.dirname(app), "eval.json"), "w"), ensure_ascii=False, indent=2)
print(json.dumps({k: out[k] for k in ("hidden", "own_tests")}, ensure_ascii=False)[:800])
print("mutation", {k: v for k, v in out["mutation"].items() if k != "survived_examples"})
