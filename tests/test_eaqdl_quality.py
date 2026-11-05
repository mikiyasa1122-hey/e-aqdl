"""差分の品質: {changed} で変えたファイルだけを静的解析する・差分カバレッジ・レビュー担当への要約。"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import MEMO_V1, ROOT, Env, build_to_review, edit_json, implement  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import gate as G  # noqa: E402
from aqdlc_core import quality as Q  # noqa: E402
from aqdlc_core.project import DEFAULTS  # noqa: E402

# 変えたファイルだけを受け取る Lint の代わり（"TODO_BAD" を含むファイルがあれば不合格）
LINT_TOOL = '''import sys
print("checked:", " ".join(sys.argv[1:]))
bad = [f for f in sys.argv[1:] if "TODO_BAD" in open(f, encoding="utf-8").read()]
for f in bad:
    print("問題:", f)
sys.exit(1 if bad else 0)
'''

# 標準ライブラリだけで行カバレッジを測り、LCOV を書く（CI に coverage を入れずに済ませるため）
COV_TOOL = '''import dis
import os
import sys
import unittest

ROOT = os.getcwd()
SRC = os.path.normcase(os.path.join(ROOT, "src"))
hits = {}


def tracer(frame, event, arg):
    f = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
    if not f.startswith(SRC):
        return None
    if event == "line":
        hits.setdefault(f, set()).add(frame.f_lineno)
    return tracer


def lines_of(code):
    out = {ln for _o, ln in dis.findlinestarts(code) if ln}
    for c in code.co_consts:
        if hasattr(c, "co_code"):
            out |= lines_of(c)
    return out


sys.settrace(tracer)
suite = unittest.defaultTestLoader.discover("tests")
res = unittest.TextTestRunner().run(suite)
sys.settrace(None)
os.makedirs("coverage", exist_ok=True)
with open(os.path.join("coverage", "lcov.info"), "w", encoding="utf-8") as out:
    for base, _d, files in os.walk("src"):
        for name in files:
            if not name.endswith(".py"):
                continue
            p = os.path.join(base, name)
            with open(p, encoding="utf-8") as fh:
                exe = lines_of(compile(fh.read(), p, "exec"))
            got = hits.get(os.path.normcase(os.path.abspath(p)), set())
            out.write("SF:" + p.replace(os.sep, "/") + "\\n")
            for ln in sorted(exe):
                out.write("DA:%d,%d\\n" % (ln, 1 if ln in got else 0))
            out.write("end_of_record\\n")
sys.exit(0 if res.wasSuccessful() else 1)
'''

MEMO_UNTESTED = MEMO_V1 + '''

def stats(memos):
    n = len(memos)
    longest = max(len(m) for m in memos)
    shortest = min(len(m) for m in memos)
    total = sum(len(m) for m in memos)
    return n, longest, shortest, total
'''

# 起点（git に記録済み）: テストのない古い関数を含む
MEMO_BASE = '''def add(memos, text):
    memos.append(text)
    return memos


def legacy(x):
    y = x + 1
    z = y * 2
    return z
'''

MEMO_BASE_CHANGED = '''def add(memos, text):
    if text == "":
        raise ValueError("空のメモ")
    memos.append(text)
    return memos


def legacy(x):
    y = x + 1
    z = y * 2
    return z
'''


def write(e, rel, text):
    p = os.path.join(e.project, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def py():
    return f'"{sys.executable}"'


def cov_config(**kw):
    dc = {"enabled": True, "report": "coverage/lcov.info", "min": 80}
    dc.update(kw)
    return {"quality": {"diff_coverage": dc},
            "checks": [{"name": "cov", "command": f"{py()} tools/cov.py", "kind": "unit"}]}


class ScopedLintTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        write(self.e, "tools/lint.py", LINT_TOOL)
        write(self.e, "legacy.py", "x = 1  # TODO_BAD（run の前からある問題）\n")

    def tearDown(self):
        self.e.cleanup()

    def config(self, ext=".py"):
        return {"checks": [{"name": "lint", "command": f"{py()} tools/lint.py {{changed:{ext}}}", "kind": "lint"}]}

    def test_only_changed_files_are_checked(self):
        e = self.e
        build_to_review(e, with_question=False, config=self.config())
        g = e.state()["gate"]
        r = g["results"]["checks"]["lint"]
        self.assertTrue(r["pass"], r)  # 前からある legacy.py の問題では止めない
        self.assertEqual(r["scoped"], 2)  # src/memo.py と tests/test_memo.py
        with open(os.path.join(e.project, r["log"]), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("src/memo.py", log)
        self.assertNotIn("legacy.py", log)
        self.assertTrue(g["checks_pass"])
        # レビュー担当には、機械で確かめた範囲を渡す
        e.spawn("reviewer")
        brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("機械で確かめた差分の品質", brief)
        self.assertIn("チェック lint（変えたファイル 2 件だけ）: 合格", brief)

    def test_new_problem_fails(self):
        e = self.e
        build_to_review(e, with_question=False, config=self.config(), memo_src=MEMO_V1 + "# TODO_BAD\n")
        g = e.state()["gate"]
        self.assertFalse(g["results"]["checks"]["lint"]["pass"])
        self.assertFalse(g["checks_pass"])

    def test_no_targets_is_recorded_not_hidden(self):
        e = self.e
        build_to_review(e, with_question=False, config=self.config(".rs"))
        r = e.state()["gate"]["results"]["checks"]["lint"]
        self.assertEqual((r["pass"], r.get("no_targets"), r["scoped"]), (True, True, 0))
        self.assertNotIn("log", r)  # 実行していない


class DiffCoverageTest(unittest.TestCase):
    def tearDown(self):
        self.e.cleanup()

    def test_untested_new_lines_fail_and_are_reported(self):
        self.e = e = Env()
        write(e, "tools/cov.py", COV_TOOL)
        build_to_review(e, with_question=False, config=cov_config(), memo_src=MEMO_UNTESTED)
        g = e.state()["gate"]
        self.assertTrue(g["results"]["checks"]["cov"]["pass"])  # テスト自体は通る
        dc = g["results"]["diff_coverage"]
        self.assertFalse(dc["pass"], dc)
        self.assertLess(dc["percent"], 80)
        unc = dc["uncovered"]["src/memo.py"]
        self.assertEqual(unc, [9, 10, 11, 12, 13])  # stats の本体（テストで実行されていない）
        self.assertNotIn(2, unc)  # add の本体は実行されている
        self.assertFalse(g["checks_pass"])
        self.assertIn("差分カバレッジ", e.next())
        # 実装担当への修正依頼に、実行されていない行を渡す
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("テストで実行されていない変更行: src/memo.py: ", brief)
        # 直すと合格
        implement(e, MEMO_V1, extra="\n## 指摘への対応\n- 差分カバレッジ: 使わない stats を消した\n")
        self.assertIsNone(e.stop("implementer", "修正完了"))
        e.aq("gate")
        dc = e.state()["gate"]["results"]["diff_coverage"]
        self.assertTrue(dc["pass"], dc)
        self.assertEqual(dc["percent"], 100.0)
        self.assertTrue(e.state()["gate"]["checks_pass"])

    def test_only_changed_lines_count(self):
        """起点（git の HEAD）からの差分の行だけを数える。テストのない古い関数では止めない。"""
        self.e = e = Env(git=True)
        write(e, "tools/cov.py", COV_TOOL)
        write(e, "src/memo.py", MEMO_BASE)
        write(e, ".gitignore", "coverage/\n__pycache__/\n.aq-dlc/\n")
        e.git("add", "-A")
        e.git("commit", "-q", "-m", "base")
        build_to_review(e, with_question=False, config=cov_config(), memo_src=MEMO_BASE_CHANGED)
        s = e.state()
        self.assertTrue(s["baseline"]["git_head"])
        dc = s["gate"]["results"]["diff_coverage"]
        self.assertTrue(dc["pass"], dc)
        self.assertEqual((dc["percent"], dc["total"]), (100.0, 2))  # 足した if と raise の2行
        self.assertEqual(dc["approx"], [])
        self.assertNotIn("src/memo.py", dc["uncovered"])  # legacy の本体は起点からある行なので数えない

    def test_missing_or_stale_report_fails(self):
        self.e = e = Env()
        cfg = cov_config()
        cfg["checks"] = []  # レポートを作るチェックがない
        os.makedirs(os.path.join(e.project, "coverage"))
        old = os.path.join(e.project, "coverage", "lcov.info")
        with open(old, "w", encoding="utf-8") as f:
            f.write("SF:src/memo.py\nDA:1,1\nend_of_record\n")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        build_to_review(e, with_question=False, config=cfg)
        dc = e.state()["gate"]["results"]["diff_coverage"]
        self.assertFalse(dc["pass"])
        self.assertIn("この gate で作られていない", dc["error"])
        os.remove(old)
        e.aq("gate")
        self.assertIn("がない", e.state()["gate"]["results"]["diff_coverage"]["error"])

    def test_unmeasured_file_fails_unless_excluded(self):
        self.e = e = Env()
        write(e, "tools/cov.py", COV_TOOL)
        build_to_review(e, with_question=False, config=cov_config())
        write(e, "scripts/run.py", "print('x')\n")  # 測っていない場所に足したファイル
        e.aq("gate")
        dc = e.state()["gate"]["results"]["diff_coverage"]
        self.assertFalse(dc["pass"])
        self.assertEqual(dc["unmeasured"], ["scripts/run.py"])
        # 対象外にするのは人の設定（設定の変更として扱われる）
        edit_json(os.path.join(e.project, ".aq-dlc", "config.json"),
                  lambda c: c["quality"]["diff_coverage"].update(exclude=["scripts/*"]))
        e.aq("gate")
        self.assertTrue(e.state()["gate"]["results"]["diff_coverage"]["pass"])


class UnitTest(unittest.TestCase):
    def test_expand(self):
        files = ["a.py", "b.ts", "c.md"]
        self.assertEqual(Q.expand("ruff check .", files, files), ("ruff check .", None))
        cmd, n = Q.expand("lint {changed:.py,ts}", files, files)
        self.assertEqual(n, 2)
        self.assertTrue(cmd.startswith("lint ") and "a.py" in cmd and "b.ts" in cmd and "c.md" not in cmd)
        self.assertEqual(Q.expand("lint {changed:.rs}", files, files), (None, 0))
        # 起点のない run（旧版）は全ファイルを対象にする（厳しい側）
        self.assertEqual(Q.expand("lint {changed:.py}", None, ["x.py", "y.py"])[1], 2)
        cmd, _n = Q.expand("lint {changed}", ["a b.py"], [])
        self.assertIn("a b.py", cmd)
        self.assertNotEqual(cmd, "lint a b.py")  # 空白を含むパスはクォートする

    def test_parse_reports(self):
        e = Env()
        try:
            j = os.path.join(e.project, "c.json")
            with open(j, "w", encoding="utf-8") as f:
                json.dump({"files": {os.path.join(e.project, "src", "m.py"): {"executed_lines": [1, 2],
                                                                            "missing_lines": [3]}}}, f)
            self.assertEqual(Q.parse_report(e.project, j), {"src/m.py": {"executable": {1, 2, 3}, "covered": {1, 2}}})
            lc = os.path.join(e.project, "l.info")
            with open(lc, "w", encoding="utf-8") as f:
                f.write("TN:\nSF:./src/m.js\nDA:1,3\nDA:2,0\nend_of_record\n")
            self.assertEqual(Q.parse_report(e.project, lc), {"src/m.js": {"executable": {1, 2}, "covered": {1}}})
            with open(lc, "w", encoding="utf-8") as f:
                f.write("hello\n")
            with self.assertRaises(ValueError):
                Q.parse_report(e.project, lc)
        finally:
            e.cleanup()

    def test_ranges(self):
        self.assertEqual(Q.ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")

    def test_default_quality_does_not_change_config_fp(self):
        import copy
        cfg = copy.deepcopy(DEFAULTS)
        no_q = {k: v for k, v in cfg.items() if k != "quality"}
        self.assertEqual(G.config_fp(cfg), G.config_fp(no_q))
        cfg["quality"]["diff_coverage"]["enabled"] = True
        self.assertNotEqual(G.config_fp(cfg), G.config_fp(no_q))


if __name__ == "__main__":
    unittest.main()
