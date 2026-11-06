"""ケースの妥当性を機械で確かめる（レビューの前に実行する）。

確かめること:
- 参照コード: 見えるテストと、すべての repro が合格する
- 欠陥ケース: 置き換えがちょうど1か所に当たる / 見えるテストは合格する（gate を通る）/ その repro が失敗する
- 欠陥なしケース: 置き換えがちょうど1か所に当たる / 見えるテストとすべての repro が合格する
使い方: python3 check_cases.py [--json 出力先]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cases as CS  # noqa: E402

APP = os.path.join(HERE, "app")


def materialize(case, dest):
    """参照コードをコピーし、ケースの置き換えを適用する。"""
    shutil.copytree(APP, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, old, new in (case["changes"] if case else []):
        p = os.path.join(dest, rel)
        with open(p, encoding="utf-8") as f:
            s = f.read()
        n = s.count(old)
        if n != 1:
            raise ValueError(f"{case['id']}: {rel} の置き換え元が {n} か所に当たる（1か所であること）: {old[:60]!r}")
        with open(p, "w", encoding="utf-8") as f:
            f.write(s.replace(old, new))


def run_unittest(cwd, target):
    p = subprocess.run([sys.executable, "-m", "unittest", target], cwd=cwd, capture_output=True, text=True, timeout=120)
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:]


def run_visible(cwd):
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=cwd,
                       capture_output=True, text=True, timeout=120)
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:]


def run_repro(cwd, defect_case):
    path = os.path.join(cwd, f"repro_{defect_case['id'].replace('-', '_').lower()}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(CS.REPRO_HEAD + defect_case["repro"] + "\n\nif __name__ == '__main__':\n    unittest.main()\n")
    try:
        return run_unittest(cwd, os.path.basename(path)[:-3])
    finally:
        os.remove(path)


def check():
    defects = [c for c in CS.CASES if c["defect"]]
    report = {"reference": {}, "cases": {}}
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        ref = os.path.join(tmp, "ref")
        materialize(None, ref)
        ok, out = run_visible(ref)
        report["reference"]["visible"] = ok
        if not ok:
            problems.append("参照コードで見えるテストが失敗: " + out)
        for d in defects:
            ok, out = run_repro(ref, d)
            report["reference"][d["id"]] = ok
            if not ok:
                problems.append(f"参照コードで {d['id']} の repro が失敗:\n{out}")
        for c in CS.CASES:
            dest = os.path.join(tmp, c["id"])
            r = report["cases"][c["id"]] = {}
            try:
                materialize(c, dest)
            except ValueError as e:
                problems.append(str(e))
                r["apply"] = False
                continue
            r["apply"] = True
            r["visible"], out = run_visible(dest)
            if not r["visible"]:
                problems.append(f"{c['id']}: 見えるテストが失敗（gate を通らない）:\n{out}")
            if c["defect"]:
                r["repro_fails"] = not run_repro(dest, c)[0]
                if not r["repro_fails"]:
                    problems.append(f"{c['id']}: 欠陥版で repro が合格する（欠陥を再現できていない）")
            else:
                bad = [d["id"] for d in defects if not run_repro(dest, d)[0]]
                r["all_repro_pass"] = not bad
                if bad:
                    problems.append(f"{c['id']}: 欠陥なしのはずが repro {bad} が失敗")
    report["problems"] = problems
    return report


if __name__ == "__main__":
    rep = check()
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    n = len(CS.CASES)
    print(f"ケース {n} 件（欠陥 {sum(c['defect'] for c in CS.CASES)} 件）")
    for p in rep["problems"]:
        print("問題:", p)
    print("OK" if not rep["problems"] else f"問題 {len(rep['problems'])} 件")
    sys.exit(1 if rep["problems"] else 0)
