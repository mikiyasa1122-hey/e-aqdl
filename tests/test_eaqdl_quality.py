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
from aqdlc_core import verdict as V  # noqa: E402
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


def _mk(e, rel):
    p = os.path.join(e.project, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


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
        g = e.state()["gate"]
        r = g["results"]["checks"]["lint"]
        # 実行していないものを「合格」と書かない。Lint は対象なしなら gate を止めない（この run の新しい問題はない）
        self.assertEqual((r["status"], r["pass"], r["optional"], r["scoped"]), ("not_applicable", False, True, 0))
        self.assertNotIn("log", r)
        self.assertTrue(g["checks_pass"])

    def test_required_check_without_targets_fails(self):
        """Lint・型チェック以外（単体テストなど）は、対象なしを合格にしない。"""
        e = self.e
        cfg = {"checks": [{"name": "newtests", "command": f"{py()} -m unittest {{changed:.rs}}", "kind": "unit"}]}
        build_to_review(e, with_question=False, config=cfg)
        g = e.state()["gate"]
        r = g["results"]["checks"]["newtests"]
        self.assertEqual((r["status"], r["pass"], r["optional"]), ("not_applicable", False, False))
        self.assertFalse(g["checks_pass"])
        self.assertIn("チェック newtests", e.next())
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("対象なしを合格にできない", brief)  # 修正依頼に理由を渡す


class RunGateContractTest(unittest.TestCase):
    """gate.run_gate の結果の契約（P0-1）: 実行していないものを合格にしない。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_gate(self, checks, verifications=()):
        import contextlib
        import copy
        import io
        cfg = copy.deepcopy(DEFAULTS)
        cfg["checks"] = checks
        cfg["gate"]["default_timeout_sec"] = 60
        state = {"baseline": {"snapshot": {}}, "requirements": [{"id": "R1", "acceptance": [
            {"id": "AC1", "verifications": list(verifications)}], "risks": []}]}

        def iv(st):
            for r in st["requirements"]:
                for p in r["acceptance"]:
                    for v in p["verifications"]:
                        yield r, p, v
        with contextlib.redirect_stdout(io.StringIO()):
            return G.run_gate(self.tmp, state, cfg, iv, os.path.join(self.tmp, ".aq-dlc", "logs"), "s1")

    def test_cases(self):
        ok_cmd = f'{py()} -c "pass"'
        ng_cmd = f'{py()} -c "import sys; sys.exit(3)"'
        res, ok = self.run_gate(
            [{"name": "lint_none", "command": f"{ok_cmd} {{changed:.rs}}", "kind": "lint"},
             {"name": "unit_ok", "command": ok_cmd, "kind": "unit"},
             {"name": "lint_ng", "command": f"{ng_cmd} {{changed:.py}}", "kind": "lint"},
             {"name": "slow", "command": f'{py()} -c "import time; time.sleep(5)"', "kind": "other", "timeout_sec": 1}],
            [{"id": "V1", "level": "unit", "command": f"{ok_cmd} {{changed:.rs}}"},
             {"id": "V2", "level": "unit", "checks": ["lint_none"]},
             {"id": "V3", "level": "unit", "checks": ["unit_ok"]},
             {"id": "V4", "level": "unit", "command": ok_cmd}])
        c, v = res["checks"], res["verifications"]
        # 対象0件の任意の Lint: 実行していない。合格とは書かないが、gate は止めない
        self.assertEqual((c["lint_none"]["status"], c["lint_none"]["pass"], c["lint_none"]["optional"]),
                         ("not_applicable", False, True))
        self.assertEqual(c["unit_ok"]["status"], "passed")  # 必須のテストが正常に実行された
        self.assertEqual((c["lint_ng"]["status"], c["lint_ng"]["scope"]), ("failed", {"mode": "changed", "targets": 1}))
        self.assertEqual(c["slow"]["status"], "error")  # 実行失敗（タイムアウト）は不合格と区別する
        self.assertIn("タイムアウト", c["slow"]["reason"])
        self.assertFalse(ok)  # lint_ng と slow で不合格
        # 対象0件の必須の検証・対象なしのチェックに頼る検証は、確かめたことにしない
        self.assertEqual((v["V1"]["status"], v["V1"]["pass"]), ("not_applicable", False))
        self.assertEqual((v["V2"]["status"], v["V2"]["pass"]), ("not_applicable", False))
        self.assertEqual(v["V3"]["status"], "passed")
        self.assertEqual(v["V4"]["status"], "passed")

    def test_optional_not_applicable_does_not_block(self):
        res, ok = self.run_gate([{"name": "lint", "command": f'{py()} -c "pass" {{changed:.rs}}', "kind": "lint"},
                                 {"name": "unit", "command": f'{py()} -c "pass"', "kind": "unit"}])
        self.assertTrue(ok)

    def test_missing_command_is_error(self):
        res, ok = self.run_gate([{"name": "x", "command": "no-such-command-aqdlc-test", "kind": "unit"}])
        # POSIX のシェルは 127（コマンドなし）→ error。Windows の cmd は 1 を返すので failed になる
        self.assertIn(res["checks"]["x"]["status"], ("error", "failed"))
        self.assertFalse(res["checks"]["x"]["pass"])
        self.assertFalse(ok)


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
        self.changed_lines_case(crlf=False)

    def test_only_changed_lines_count_with_crlf_checkout(self):
        """作業ツリーの改行が CRLF（Windows の core.autocrlf）でも、起点の内容と対応づけられる。"""
        self.changed_lines_case(crlf=True)

    def changed_lines_case(self, crlf):
        self.e = e = Env(git=True)
        write(e, "tools/cov.py", COV_TOOL)
        if crlf:
            e.git("config", "core.autocrlf", "true")
            with open(_mk(e, "src/memo.py"), "wb") as f:
                f.write(MEMO_BASE.replace("\n", "\r\n").encode("utf-8"))
        else:
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
        self.assertIn("この gate で作られていない", dc["reason"])
        self.assertFalse(os.path.exists(old))  # gate の前に前回のレポートを消している

    def test_report_written_just_before_gate_is_not_used(self):
        """gate の直前（1秒以内）に作られたレポートも、この gate の証拠として使わない。"""
        self.e = e = Env()
        cfg = cov_config()
        cfg["checks"] = []
        build_to_review(e, with_question=False, config=cfg)
        p = os.path.join(e.project, "coverage", "lcov.info")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("SF:src/memo.py\n" + "".join(f"DA:{i},1\n" for i in range(1, 30)) + "end_of_record\n")
        e.aq("gate")
        dc = e.state()["gate"]["results"]["diff_coverage"]
        self.assertEqual(dc["status"], "error")
        self.assertIn("この gate で作られていない", dc["reason"])

    def test_broken_report_fails_and_fresh_report_has_evidence(self):
        self.e = e = Env()
        write(e, "tools/cov.py", COV_TOOL)
        build_to_review(e, with_question=False, config=cov_config())
        dc = e.state()["gate"]["results"]["diff_coverage"]
        self.assertEqual(dc["status"], "passed")
        with open(os.path.join(e.project, "coverage", "lcov.info"), "rb") as f:
            import hashlib
            self.assertEqual(dc["evidence"]["sha1"], hashlib.sha1(f.read()).hexdigest())  # この gate のレポート
        self.assertEqual(dc["evidence"]["gate_run"], e.state()["gate"]["stamp"])
        # 壊れたレポートを作るチェックに変える
        edit_json(os.path.join(e.project, ".aq-dlc", "config.json"), lambda c: c["checks"].__setitem__(
            next(i for i, x in enumerate(c["checks"]) if x["name"] == "cov"),
            {"name": "cov", "kind": "unit", "command": f'{py()} -c "import os;os.makedirs(\'coverage\',exist_ok=True);'
             f'open(\'coverage/lcov.info\',\'w\').write(\'garbage\')"'}))
        e.aq("gate")
        dc = e.state()["gate"]["results"]["diff_coverage"]
        self.assertEqual((dc["status"], dc["pass"]), ("error", False))
        self.assertIn("読めない", dc["reason"])
        self.assertFalse(e.state()["gate"]["checks_pass"])

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


class VerdictTest(unittest.TestCase):
    """合否の規則（外部コマンドなしで確かめる。AC-05）。"""

    def test_from_run(self):
        self.assertEqual(V.from_run(0, "", 1, "l", "c")["status"], "passed")
        self.assertEqual(V.from_run(1, "", 1, "l", "c")["status"], "failed")
        self.assertEqual(V.from_run(124, "タイムアウト（5秒）", 5, "l", "c")["status"], "error")
        self.assertEqual(V.from_run(124, "exit 124 from tool", 1, "l", "c")["status"], "failed")
        self.assertEqual(V.from_run(127, "", 1, "l", "c")["status"], "error")
        self.assertEqual(V.from_run(0, "", 1, "l", "c", 3)["scope"], {"mode": "changed", "targets": 3})

    def test_no_targets_and_check_ok(self):
        lint = V.no_targets("lint", "c", required=False)
        unit = V.no_targets("unit", "c", required=False)
        ver = V.no_targets("lint", "c", required=True)
        self.assertEqual([r["pass"] for r in (lint, unit, ver)], [False, False, False])  # 実行していないものは合格ではない
        self.assertEqual([V.check_ok(r) for r in (lint, unit, ver)], [True, False, False])
        self.assertTrue(V.checks_pass({"a": lint, "b": V.from_run(0, "", 1, "l", "c")}, True))
        self.assertFalse(V.checks_pass({"a": unit}, True))
        self.assertFalse(V.checks_pass({}, False))  # チェックが1つもなければ不合格

    def test_via_checks(self):
        ok = V.from_run(0, "", 1, "l", "c")
        na = V.no_targets("lint", "c", required=False)
        self.assertEqual(V.via_checks(["a"], {"a": ok})["status"], "passed")
        self.assertEqual(V.via_checks(["a"], {"a": na})["status"], "not_applicable")
        self.assertEqual(V.via_checks(["a", "z"], {"a": ok})["status"], "failed")

    def test_legacy_records(self):
        """旧版（status のない記録）の読み替え。"""
        self.assertEqual(V.status_of({"pass": True}), "passed")
        self.assertEqual(V.status_of({"pass": False}), "failed")
        self.assertEqual(V.status_of({"pass": False, "skipped": "x"}), "skipped")
        self.assertEqual(V.status_of({"pass": False, "fail_fast": True, "skipped": "x"}), "skipped")
        self.assertEqual(V.status_of({"pass": False, "pending_after_merge": True}), "pending")
        self.assertEqual(V.status_of({"pass": False, "manual": True}), "manual")
        self.assertEqual(V.status_of({"pass": False, "manual": False, "error": "未設定"}), "error")
        self.assertEqual(V.status_of({"pass": True, "no_targets": True}), "not_applicable")  # 0.6.6 の開発版
        self.assertFalse(V.check_ok({"pass": True, "no_targets": True}))  # optional の印がなければ止める（安全側）
        self.assertTrue(V.check_ok({"pass": True}))

    def test_diff_coverage_rules(self):
        m = {"total": 10, "covered": 9, "files": 1, "uncovered": {"a.py": [3]}, "unmeasured": [], "approx": []}
        self.assertEqual(V.diff_coverage(m, 80, "r")["status"], "passed")
        self.assertEqual(V.diff_coverage(dict(m, covered=7), 80, "r")["status"], "failed")
        self.assertEqual(V.diff_coverage(dict(m, unmeasured=["b.py"]), 80, "r")["status"], "failed")
        self.assertEqual(V.diff_coverage({"error": "ない"}, 80, "r")["status"], "error")
        na = V.diff_coverage(dict(m, total=0, covered=0, uncovered={}), 80, "r")
        self.assertEqual((na["status"], na["pass"]), ("not_applicable", False))
        f = V.failures({"checks": {}, "diff_coverage": na})
        self.assertIsNone(f["diff_coverage"])  # 計測の対象になる行がないことは gate を止めない
        self.assertTrue(V.overall({"checks": {"u": V.from_run(0, "", 1, "l", "c")}, "diff_coverage": na}, True))
        self.assertFalse(V.overall({"checks": {"u": V.from_run(0, "", 1, "l", "c")},
                                    "diff_coverage": V.diff_coverage({"error": "x"}, 80, "r")}, True))
        self.assertFalse(V.overall({"checks": {"u": V.from_run(0, "", 1, "l", "c")},
                                    "holdout": {"AC1": V.from_holdout(1, 1)}}, True))


class DependencyTest(unittest.TestCase):
    """gate と quality の間に循環がない（AC-01）。規則のモジュールはパッケージ内の他のモジュールに依存しない。"""

    def imports(self, name):
        import ast
        path = os.path.join(ROOT, "plugins", "aq-dlc", "scripts", "aqdlc_core", name + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level == 1:
                out.add(n.module or "")
                out.update(a.name for a in n.names if not n.module)
        return out

    def test_no_cycle_between_gate_and_quality(self):
        self.assertNotIn("gate", self.imports("quality"))
        self.assertIn("quality", self.imports("gate"))

    def test_rule_modules_are_leaves(self):
        for m in ("verdict", "decision_sheet", "fileclass"):
            self.assertEqual(self.imports(m), set(), m)
        self.assertEqual(self.imports("quality"), {"fileclass", "util"})

    def test_modules_import_alone(self):
        import subprocess
        scripts = os.path.join(ROOT, "plugins", "aq-dlc", "scripts")
        for m in ("quality", "gate", "verdict", "gate_service"):
            r = subprocess.run([sys.executable, "-c", f"import aqdlc_core.{m}"], cwd=scripts, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
