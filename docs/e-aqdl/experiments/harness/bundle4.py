"""実験04: 各 run の記録を束ねる（report.md / diff.txt / tests.txt）。条件名は伏せた ID で出力する。

使い方: python3 bundle4.py <exp4 のフォルダ> <出力フォルダ> <条件名=伏せID> ...
"""
import json
import os
import subprocess
import sys

root, out = sys.argv[1], sys.argv[2]
for pair in sys.argv[3:]:
    cond, bid = pair.split("=")
    app = os.path.join(root, cond, "app")
    dst = os.path.join(out, bid)
    os.makedirs(dst, exist_ok=True)
    s = json.load(open(os.path.join(root, cond, "logs", "summary.json"), encoding="utf-8"))
    lines = []
    for i, t in enumerate(s["turns"]):
        if i > 0:
            lines.append(f"## 人の返事 {i}\n\n{t['input']}\n")
        lines.append(f"## AI の報告 {i + 1}\n\n{t['result']}\n")
    open(os.path.join(dst, "report.md"), "w", encoding="utf-8").write("\n".join(lines))
    first = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=app, capture_output=True,
                           text=True).stdout.split()[0]
    subprocess.run(["git", "add", "-A", "--", ".", ":!.aq-dlc", ":!CLAUDE.md", ":!.claude"], cwd=app,
                   capture_output=True)
    diff = subprocess.run(["git", "diff", "--cached", first, "--", ".", ":!.aq-dlc", ":!CLAUDE.md", ":!.claude"],
                          cwd=app, capture_output=True, text=True).stdout
    subprocess.run(["git", "reset", "-q"], cwd=app)
    open(os.path.join(dst, "diff.txt"), "w", encoding="utf-8").write(diff)
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=app,
                       capture_output=True, text=True, timeout=600,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    q = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=app,
                       capture_output=True, text=True, timeout=600,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    open(os.path.join(dst, "tests.txt"), "w", encoding="utf-8").write(
        f"## python3 -m unittest discover -s tests -t .\nexit={p.returncode}\n{p.stderr[-5000:]}\n\n"
        f"## python3 -m pytest -q\nexit={q.returncode}\n{q.stdout[-5000:]}")
    print(cond, "->", bid, "diff", len(diff), "test exit", p.returncode)
