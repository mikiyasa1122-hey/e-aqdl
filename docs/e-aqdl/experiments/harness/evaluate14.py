"""実験14 の評価（作り手から独立に測る）。

使い方: python3 -I evaluate14.py <作業フォルダ> <起点のコミット>
出力: JSON（変えたファイル・既存ファイルの変更・新しい Lint の問題・差分カバレッジ・隠しテスト）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

app, base = sys.argv[1], sys.argv[2]
HIDDEN = "/root/.exp-hidden/exp14/test_hidden.py"
SKIP = (".aq-dlc/", ".claude/", "CLAUDE.md", ".gitignore")
PRE = {"inventory/report.py", "inventory/client.py", "inventory/__init__.py", "tests/test_client.py",
       "tests/test_live.py", "pyproject.toml", "README.md"}


def sh(cmd, cwd=app):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


tracked = [f for f in sh(["git", "diff", "--name-only", base]).stdout.split() if f]
untracked = [f for f in sh(["git", "ls-files", "-o", "--exclude-standard"]).stdout.split() if f]
changed = sorted(f for f in set(tracked + untracked) if not f.startswith(SKIP) and os.path.exists(os.path.join(app, f)))
py = [f for f in changed if f.endswith(".py")]

out = {"changed": changed, "pre_existing_modified": sorted(set(changed) & PRE)}

# 新しい Lint の問題（変えた .py だけ）と、リポジトリ全体
def ruff(files):
    if files is not None and not files:
        return []
    r = sh(["ruff", "check", "--output-format", "json", "--no-cache"] + (files or ["."]))
    try:
        return [f"{os.path.relpath(d['filename'], app)}:{d['location']['row']} {d['code']}" for d in json.loads(r.stdout)]
    except ValueError:
        return ["ruff の出力を読めない: " + r.stdout[:200] + r.stderr[:200]]


out["lint_changed"] = ruff(py)
out["lint_repo_total"] = len(ruff(None))
out["report_py_lint"] = len(ruff(["inventory/report.py"]))

# 変えた行（テスト以外の .py）
def added_lines(f):
    if f in untracked:
        with open(os.path.join(app, f), encoding="utf-8", errors="replace") as fh:
            return set(range(1, len(fh.read().splitlines()) + 1))
    lines = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", sh(["git", "diff", "-U0", base, "--", f]).stdout, re.M):
        start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        lines.update(range(start, start + n))
    return lines


src = [f for f in py if not re.search(r"(^|/)tests?/|test_", f)]
tmp = tempfile.mkdtemp(prefix="eval14-")
try:
    work = os.path.join(tmp, "app")
    shutil.copytree(app, work, ignore=shutil.ignore_patterns(".git", ".aq-dlc", "__pycache__", "coverage"))
    mods = sorted("tests." + os.path.basename(p)[:-3] for p in os.listdir(os.path.join(work, "tests"))
                  if p.startswith("test_") and p.endswith(".py") and p != "test_live.py")
    r = sh(["python3", "-m", "coverage", "run", "--source=inventory", "-m", "unittest"] + mods, cwd=work)
    out["visible_tests_ok"] = r.returncode == 0
    out["visible_tests_tail"] = (r.stdout + r.stderr).strip().splitlines()[-1:] if r.returncode else []
    sh(["python3", "-m", "coverage", "json", "-o", "cov.json", "-q"], cwd=work)
    try:
        cov = json.load(open(os.path.join(work, "cov.json")))["files"]
    except (OSError, ValueError, KeyError):
        cov = {}
    total = hit = 0
    unc = {}
    for f in src:
        c = cov.get(f) or cov.get(os.path.join(work, f))
        lines = added_lines(f)
        if c is None:
            unc[f] = "計測されていない"
            continue
        exe = lines & (set(c["executed_lines"]) | set(c["missing_lines"]))
        h = exe & set(c["executed_lines"])
        total += len(exe)
        hit += len(h)
        if exe - h:
            unc[f] = sorted(exe - h)
    out["diff_cov"] = {"total": total, "covered": hit, "percent": round(hit * 100 / total, 1) if total else None,
                       "uncovered": unc}
    r = sh(["python3", "-I", HIDDEN], cwd=work)
    m = re.search(r"Ran (\d+) test", r.stderr)
    fails = re.findall(r"^(?:FAIL|ERROR): (\w+)", r.stderr, re.M)
    out["hidden"] = {"ran": int(m.group(1)) if m else 0, "failed": fails, "ok": r.returncode == 0}
finally:
    shutil.rmtree(tmp, ignore_errors=True)

out["test_live_unchanged"] = sh(["git", "diff", "--quiet", base, "--", "tests/test_live.py"]).returncode == 0 \
    and os.path.exists(os.path.join(app, "tests/test_live.py"))
print(json.dumps(out, ensure_ascii=False, indent=1))
