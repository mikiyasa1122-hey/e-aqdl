"""ケースの妥当性を機械で確かめる（レビューの前に実行する）。

確かめること:
- 参照コード: 見えるテストと、すべての repro が合格する
- 欠陥ケース: 置き換えがちょうど1か所に当たる / 見えるテストは合格する（gate を通る）/ その repro が失敗する
- 欠陥なしケース: 置き換えがちょうど1か所に当たる / 見えるテストとすべての repro が合格する
- クリーンケース群（--set clean。clean_cases.py）: 各ケースでテストがすべて合格し、30件の欠陥の repro と
  残っていた欠陥の repro もすべて合格する。参照コードに30件の欠陥を1つずつ入れると、テストが失敗する（テストの強さ）
- SQL の追加ケース（--set sql。sql_cases.py）: 既存のケースと同じ確かめ方をする
使い方: python3 check_cases.py [--set clean|sql] [--json 出力先]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cases as CS  # noqa: E402
import clean_cases as CL  # noqa: E402
import sql_cases as SQ  # noqa: E402
import tested_cases as TD  # noqa: E402

APP = os.path.join(HERE, "app")


def all_cases():
    return CS.CASES + CL.CASES + SQ.CASES + TD.CASES


def materialize(case, dest, app=None, changes=None):
    """参照コードをコピーし、ケースの置き換えを適用する（クリーンケース群は app_clean を使う）。"""
    src = os.path.join(HERE, app) if app else os.path.join(HERE, case["app"]) if case and case.get("app") else APP
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, old, new in (changes if changes is not None else case["changes"] if case else []):
        p = os.path.join(dest, rel)
        with open(p, encoding="utf-8") as f:
            s = f.read()
        n = s.count(old)
        if n != 1:
            raise ValueError(f"{case['id']}: {rel} の置き換え元が {n} か所に当たる（1か所であること）: {old[:60]!r}")
        with open(p, "w", encoding="utf-8") as f:
            f.write(s.replace(old, new))
    if case and case.get("drop_failing_tests"):
        return prune_failing_tests(dest)
    return []


def failing_tests(cwd):
    """tests/ のうち、失敗・エラーになるテストの ID（tests.<module>.<Class>.<method>）。"""
    runner = ("import json,unittest\n"
              "s=unittest.defaultTestLoader.discover('tests',top_level_dir='.')\n"
              "r=unittest.TestResult();s.run(r)\n"
              "print(json.dumps([t.id() for t,_ in r.failures+r.errors]))\n")
    # 作業フォルダに __pycache__ を残さない（ケースのファイルとして取り込まれないように）
    p = subprocess.run([sys.executable, "-B", "-c", runner], cwd=cwd, capture_output=True, text=True, timeout=180,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return json.loads(p.stdout.strip().splitlines()[-1])


def prune_failing_tests(dest):
    """欠陥で失敗するテストのメソッドだけを取り除く（残りはすべて合格する）。取り除いた ID の列を返す。"""
    import ast
    dropped = []
    for _ in range(3):
        bad = failing_tests(dest)
        if not bad:
            return dropped
        by_file = {}
        for tid in bad:
            m = re.match(r"tests\.(\w+)\.(\w+)\.(\w+)(?: \(.*\))?$", tid, re.S)  # subTest は「method (引数)」
            if not m:
                raise ValueError(f"テストの ID を読めない（読み込みの失敗など）: {tid}")
            by_file.setdefault(m.group(1), set()).add((m.group(2), m.group(3)))
        for mod, items in by_file.items():
            path = os.path.join(dest, "tests", mod + ".py")
            with open(path, encoding="utf-8") as f:
                src = f.read()
            lines = src.splitlines(keepends=True)
            cut = []
            for node in ast.parse(src).body:
                if not isinstance(node, ast.ClassDef):
                    continue
                gone = [n for n in node.body if isinstance(n, ast.FunctionDef) and (node.name, n.name) in items]
                tests_left = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")
                              and n not in gone]
                for n in ([node] if gone and not tests_left else gone):
                    first = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    cut.append((first - 1, n.end_lineno))
            for a, b in sorted(cut, reverse=True):
                while b < len(lines) and not lines[b].strip():
                    b += 1  # 後ろの空行も取る
                del lines[a:b]
            with open(path, "w", encoding="utf-8") as f:
                f.write("".join(lines).rstrip("\n") + "\n")
        dropped += sorted({re.sub(r" \(.*\)$", "", t, flags=re.S) for t in bad})
    raise ValueError(f"テストを取り除いても合格しない: {failing_tests(dest)}")


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


def check_tested():
    """「検証あり・欠陥あり」のケース群の確認。"""
    report = {"cases": {}}
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        ref = os.path.join(tmp, "ref")
        materialize(None, ref, app=TD.APP_DIR)
        for c in TD.CASES:
            r = report["cases"][c["id"]] = {}
            if not run_repro(ref, c)[0]:
                problems.append(f"{c['id']}: 参照コードで repro が失敗する")
            dest = os.path.join(tmp, c["id"])
            try:
                r["dropped_tests"] = materialize(c, dest)
            except ValueError as e:
                problems.append(f"{c['id']}: {e}")
                continue
            r["tests_pass"], out = run_visible(dest)
            m = re.search(r"Ran (\d+) test", out)
            r["tests_left"] = int(m.group(1)) if m else 0
            r["repro_fails"] = not run_repro(dest, c)[0]
            if not r["tests_pass"]:
                problems.append(f"{c['id']}: 残したテストが失敗:\n{out}")
            if not r["dropped_tests"]:
                problems.append(f"{c['id']}: 欠陥で失敗するテストがない（app_clean のテストが欠陥を捉えていない）")
            if r["tests_left"] < 50:
                problems.append(f"{c['id']}: 残ったテストが少ない（{r['tests_left']} 件）")
            if not r["repro_fails"]:
                problems.append(f"{c['id']}: 欠陥を入れても repro が合格する")
    report["problems"] = problems
    return report


def check_clean():
    """クリーンケース群の確認。"""
    defects = [c for c in CS.CASES if c["defect"]]
    residual = [{"id": k, "repro": v} for k, v in CL.RESIDUAL_REPROS.items()]
    report = {"reference": {}, "cases": {}, "mutations": {}}
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        # テストの強さ: 参照コードに30件の欠陥を1つずつ入れると、テストが失敗する
        for d in defects:
            dest = os.path.join(tmp, "mut-" + d["id"])
            changes = CL.MUTATIONS_ADAPTED.get(d["id"], d["changes"])
            try:
                materialize(None, dest, app=CL.APP_DIR, changes=changes)
            except ValueError as e:
                problems.append(f"欠陥 {d['id']} を参照コードに入れられない: {e}")
                continue
            killed = not run_visible(dest)[0]
            report["mutations"][d["id"]] = "killed" if killed else "survived"
            if not killed:
                problems.append(f"欠陥 {d['id']} を入れてもテストが合格する（テストが弱い）")
        # 残っていた欠陥の repro は、元の参照コード（app）では失敗する（repro が欠陥を捉えている）
        old = os.path.join(tmp, "old-app")
        materialize(None, old)
        for r in residual:
            if run_repro(old, r)[0]:
                problems.append(f"{r['id']}: 元の参照コードでも repro が合格する（欠陥を再現できていない）")
        for c in CL.CASES:
            dest = os.path.join(tmp, c["id"])
            r = report["cases"][c["id"]] = {}
            try:
                materialize(c, dest)
            except ValueError as e:
                problems.append(str(e))
                r["apply"] = False
                continue
            r["apply"] = True
            r["tests"], out = run_visible(dest)
            if not r["tests"]:
                problems.append(f"{c['id']}: テストが失敗:\n{out}")
            bad = [d["id"] for d in defects + residual if not run_repro(dest, d)[0]]
            r["all_repro_pass"] = not bad
            if bad:
                problems.append(f"{c['id']}: 欠陥なしのはずが repro {bad} が失敗")
    report["problems"] = problems
    return report


if __name__ == "__main__":
    if "--set" in sys.argv and sys.argv[sys.argv.index("--set") + 1] == "tested":
        rep = check_tested()
        if "--json" in sys.argv:
            with open(sys.argv[sys.argv.index("--json") + 1], "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
        for k, v in rep["cases"].items():
            print(k, f"残り {v.get('tests_left')} 件 / 取り除いた {len(v.get('dropped_tests', []))} 件")
        for p in rep["problems"]:
            print("問題:", p)
        print("OK" if not rep["problems"] else f"問題 {len(rep['problems'])} 件")
        sys.exit(1 if rep["problems"] else 0)
    if "--set" in sys.argv and sys.argv[sys.argv.index("--set") + 1] == "clean":
        rep = check_clean()
        if "--json" in sys.argv:
            with open(sys.argv[sys.argv.index("--json") + 1], "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
        killed = sum(1 for v in rep["mutations"].values() if v == "killed")
        print(f"クリーンケース {len(CL.CASES)} 件 / テストが検出した欠陥 {killed}/{len(rep['mutations'])}")
        for p in rep["problems"]:
            print("問題:", p)
        print("OK" if not rep["problems"] else f"問題 {len(rep['problems'])} 件")
        sys.exit(1 if rep["problems"] else 0)
    if "--set" in sys.argv and sys.argv[sys.argv.index("--set") + 1] in ("sql", "with-sql"):
        CS.CASES = CS.CASES + SQ.CASES  # 追加ケースも、既存のケースと同じ確かめ方をする
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
