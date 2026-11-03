"""隠し受け入れテスト（holdout）・ブラインドレビュー・レビューのモデル・Memory の既定・1ページの完了報告。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (DESIGN_MD, MEMO_V1, MEMO_V2, REVIEW_PASS, SPEC_MD, Env, build_to_review, denied,  # noqa: E402
                     edit_json, implement)

HOLDOUT_TEST = '''import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import memo  # noqa: E402


class H(unittest.TestCase):
    def test_blank_rejected(self):
        with self.assertRaises(ValueError):
            memo.add([], "   ")  # 空白だけのメモも拒否する（見えるテストにはない観点）


if __name__ == "__main__":
    unittest.main()
'''


def build_with_holdout(e, holdout_src=HOLDOUT_TEST, extra_config=None):
    """仕様承認 → 設計（holdout を登録）→ 実装 → gate まで。"""
    py = f'"{sys.executable}"'
    e.aq("init", "メモ")
    cfg = {"holdout": {"enabled": True}}
    cfg.update(extra_config or {})
    edit_json(os.path.join(e.project, ".aq-dlc", "config.json"), lambda c: c.update(cfg))
    e.aq("new", "メモ")
    e.spawn("requirements")
    e.start("requirements")
    e.as_role("requirements", "req", "R1", "メモを追加できる", "--critical")
    e.as_role("requirements", "req", "R2", "1秒以内に応答する", "--kind", "nonfunctional")
    e.as_role("requirements", "accept", "R1", "AC1", "空のメモは保存されない")
    e.as_role("requirements", "accept", "R2", "AC2", "応答が1秒以内")
    e.as_role("requirements", "risk", "R1", "K1", "空のメモが保存される")
    e.as_role("requirements", "verify", "AC1", "V1", "追加の単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "K1", "V2", "空のメモを拒否する単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "AC2", "V3", "全テストが通る", "--level", "unit")
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project), SPEC_MD)
    assert e.stop("requirements", "要求を整理") is None
    e.approve_spec()
    e.spawn("design")
    e.start("design")
    s = e.state()
    e.as_role("design", "design-map", "R1", "src/memo.py の add")
    e.as_role("design", "design-map", "R2", "src/memo.py")
    e.as_role("design", "add-check", "unit", f"{py} -m unittest discover -s tests -q", "--kind", "unit")
    e.write_as("design", os.path.relpath(os.path.join(e.arts, "design.md"), e.project),
               DESIGN_MD.format(spec=s["spec"]["registered_hash"][:8]))
    res = e.stop("design", "設計完了")
    assert res and res.get("decision") == "block" and "holdout" in res.get("reason", ""), res  # holdout がまだない
    hdir = e.as_role("design", "holdout", "--dir").strip().splitlines()[-1]
    path = os.path.join(hdir, "test_ac1.py")
    res = e.hook("pre-write", {"tool_name": "Write", "tool_input": {"file_path": path, "content": holdout_src},
                               **e.agent_fields("design")})
    assert not denied(res), res  # 設計担当は holdout の置き場所に書ける
    with open(path, "w", encoding="utf-8") as f:
        f.write(holdout_src)
    e.as_role("design", "holdout", "AC1", "--command", f"{py} {{holdout}}/test_ac1.py")
    assert e.stop("design", "設計完了") is None
    e.spawn("implementer")
    brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
    return hdir, path, brief


class HoldoutTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_disabled_by_default(self):
        e = self.e
        build_to_review(e, with_question=False)
        e.aq("holdout", "AC1", "--command", "true", ok=False)
        self.assertIn("無効", e.aq("holdout", "--list"))

    def test_implementer_cannot_read_and_gate_reports_only_ac(self):
        e = self.e
        hdir, path, brief = build_with_holdout(e)
        self.assertIn("隠し受け入れテスト", brief)
        self.assertIn("AC1", brief)
        self.assertNotIn("test_blank_rejected", brief)  # 中身は渡さない
        # 実装担当は読めない・書けない（Read・Grep・Glob・Bash・Write）
        imp = e.agent_fields("implementer")
        for tool, ti in (("Read", {"file_path": path}), ("Grep", {"pattern": "memo", "path": hdir}),
                         ("Glob", {"pattern": "*.py", "path": hdir})):
            self.assertTrue(denied(e.hook("pre-read", {"tool_name": tool, "tool_input": ti, **imp})), tool)
        self.assertTrue(denied(e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": f"cat {path}"},
                                                   **imp})))
        self.assertTrue(denied(e.hook("pre-write", {"tool_name": "Write",
                                                    "tool_input": {"file_path": path, "content": "x"}, **imp})))
        # プロジェクト内のファイルは読める
        self.assertFalse(denied(e.hook("pre-read", {"tool_name": "Read",
                                                    "tool_input": {"file_path": os.path.join(e.project, "README.md")},
                                                    **imp})))
        implement(e, MEMO_V1)  # 空白だけのメモを通してしまう実装
        assert e.stop("implementer", "実装完了") is None
        out = e.aq("gate")
        self.assertIn("隠しテスト:AC1", out)
        g = e.state()["gate"]
        self.assertFalse(g["checks_pass"])  # 見えるテストは通るが、holdout で不合格
        self.assertTrue(all(r["pass"] for r in g["results"]["checks"].values()))
        self.assertFalse(g["results"]["holdout"]["AC1"]["pass"])
        logs = [os.path.join(hdir, "logs", f) for f in os.listdir(os.path.join(hdir, "logs"))]
        with open(logs[0], encoding="utf-8") as fh:
            self.assertIn("AssertionError", fh.read())  # テストが読み込めずに落ちたのではなく、欠陥を検出して落ちた
        self.assertIn("隠しテスト", e.next())
        # 実装担当への修正依頼は、受け入れ条件の ID だけ（テストの中身・出力は渡さない）
        e.spawn("implementer")
        brief2 = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("隠しテスト AC1", brief2)
        self.assertNotIn("test_blank_rejected", brief2)
        self.assertNotIn("   ", brief2.split("今回の修正依頼")[-1].split("##")[0].replace("    ", ""))
        # holdout の出力はプロジェクトの gate ログに書かない
        for f in os.listdir(os.path.join(e.run_dir, "gate")):
            with open(os.path.join(e.run_dir, "gate", f), encoding="utf-8", errors="replace") as fh:
                self.assertNotIn("test_blank_rejected", fh.read())
        # 直した実装で合格
        implement(e, MEMO_V2, extra="\n## 指摘への対応\n- 隠しテスト AC1: 空白だけのメモも拒否するようにした\n")
        assert e.stop("implementer", "修正完了") is None
        e.aq("gate")
        g = e.state()["gate"]
        self.assertTrue(g["checks_pass"])
        self.assertTrue(g["results"]["holdout"]["AC1"]["pass"])
        # holdout を変えたら、gate の結果は古くなる
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n# 変更\n")
        self.assertIn("隠し受け入れテスト", e.next())


class BlindReviewAndModelTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_blind_review_by_default(self):
        e = self.e
        build_to_review(e, with_question=False)
        e.spawn("reviewer")
        brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ブラインドレビュー", brief)
        self.assertNotIn("impl.md`（参考", brief)
        rv = e.agent_fields("reviewer")
        impl = os.path.join(e.arts, "impl.md")
        self.assertTrue(denied(e.hook("pre-read", {"tool_name": "Read", "tool_input": {"file_path": impl}, **rv})))
        self.assertTrue(denied(e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": f"cat {impl}"},
                                                   **rv})))
        spec = os.path.join(e.arts, "spec.md")
        self.assertFalse(denied(e.hook("pre-read", {"tool_name": "Read", "tool_input": {"file_path": spec}, **rv})))
        # 実装担当・統括は実装報告を読める
        self.assertFalse(denied(e.hook("pre-read", {"tool_name": "Read", "tool_input": {"file_path": impl}})))
        self.assertIsNone(e.stop("reviewer", REVIEW_PASS))

    def test_blind_off(self):
        e = self.e
        build_to_review(e, with_question=False, config={"review": {"blind": False}})
        e.spawn("reviewer")
        brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("impl.md`（参考", brief)
        impl = os.path.join(e.arts, "impl.md")
        self.assertFalse(denied(e.hook("pre-read", {"tool_name": "Read", "tool_input": {"file_path": impl},
                                                    **e.agent_fields("reviewer")})))

    def test_reviewer_model(self):
        e = self.e
        build_to_review(e, with_question=False, config={"review": {"model": "opus"}})
        res = e.spawn("reviewer")
        self.assertEqual(res["hookSpecificOutput"]["updatedInput"]["model"], "opus")
        self.assertEqual(e.state()["agents"]["models"]["reviewer"], "opus")


class MemoryDefaultAndReportTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_memory_scope_default_is_project_and_overridable(self):
        e = self.e
        e.aq("init", "x")
        e.aq("new", "x")
        ms = e.state()["memory_scope"]
        self.assertEqual((ms["choice"], ms["source"]), ("project", "default"))  # 聞かずに進める
        self.assertNotIn("Memory の利用範囲", e.next())
        e.aq("memory-scope", "none")  # 既定値は変えられる（人間が別の範囲を求めたとき）
        self.assertEqual(e.state()["memory_scope"]["choice"], "none")
        e.aq("memory-scope", "all", ok=False)  # 共通 Memory は承認が必要なまま

    def test_explicit_config_cannot_be_overridden(self):
        e = self.e
        e.aq("init", "x")
        edit_json(os.path.join(e.project, ".aq-dlc", "config.json"),
                  lambda c: c.update(memory_policy={"scope_default": "project"}))
        e.aq("new", "x")
        self.assertEqual(e.state()["memory_scope"]["source"], "config")
        e.aq("memory-scope", "none", ok=False)

    def test_report(self):
        e = self.e
        build_to_review(e, with_question=False)
        out = e.aq("report")
        self.assertIn("完了報告", out)
        self.assertIn("証拠の状態", out)
        self.assertIn("料金ではない", out)
        self.assertIn("証明」ではありません", out)


if __name__ == "__main__":
    unittest.main()
