"""改善B: 並行処理・共有リソースのリスクを分類し、種類のあるときだけ詳しい確認を求める。

受け入れ条件
- 事前の確認の後に他の処理が対象を変える・消すケースを、レビューの観点として扱える（TOCTOU など）
- 複数の書き手がある変更で、競合シナリオと不変条件が記録される（リスクとレビューの報告の両方）
- 並行処理と関係のない変更では、テスト・処理を増やさない
- 代表的な競合の例について、期待する失敗・ロールバック・最終状態を確かめるテストがある（templates/examples/concurrency）
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Env, blocked, build_to_review, denied  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from test_review_actions import report  # noqa: E402

K1 = ("--concurrency", "toctou,lost_update", "--scenario", "A が存在を確認 → B が削除 → A が更新",
      "--invariant", "削除したメモに更新が残らない")
V2 = ("--ordering", "before_write の差し込み口で、確認と更新の間に削除を入れる")
FIX = ("- [重大1] 区分: 実装 / 対象: R1, K1 / 対応区分: 修正必須 / 内容: 確認の後の削除を見逃す"
       " / 成立条件: 確認と更新の間に削除 / 影響: 削除したメモが復活する / 根拠: src/memo.py:3"
       " / 順序: A 確認 → B 削除 → A 更新 / 不変条件: 削除したメモに更新が残らない"
       " / 再現テスト: 差し込み口で削除を挟む / 提案: 条件付き更新にする")
EVID = ("- [重大1] 区分: テスト / 対象: K1 / 対応区分: 証拠不足 / 内容: 順序を制御したテストがない"
        " / 根拠: K1 は重要な要求の競合 / 提案: 差し込み口で削除を挟むテスト")


def scen(result):
    return ("### 競合シナリオ\n- K1: 順序: A 確認 → B 削除 → A 更新 / 不変条件: 削除したメモに更新が残らない"
            f" / 結果: {result} / 根拠: src/memo.py の add を読んだ\n")


def full(verdict, b, m, findings, extra=""):
    return report(verdict, b, m, findings).replace("- AC1: 妥当\n", "- AC1: 妥当\n- AC2: 妥当\n- K1: 妥当\n") + extra


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.aq("init", "メモ")
        self.e.aq("new", "メモ")
        self.e.spawn("requirements")
        self.e.start("requirements")
        self.e.as_role("requirements", "req", "R1", "メモを追加できる")

    def tearDown(self):
        self.e.cleanup()

    def test_kinds_are_validated(self):
        out = self.e.as_role("requirements", "risk", "R1", "K1", "x", "--concurrency", "race", ok=False)
        self.assertIn("種類が不正", out)
        self.e.as_role("requirements", "risk", "R1", "K1", "x", *K1)
        k = self.e.state()["requirements"][0]["risks"][0]
        self.assertEqual((k["concurrency"], k["invariant"]), (["toctou", "lost_update"], "削除したメモに更新が残らない"))

    def test_graph_requires_scenario_invariant_and_ordering(self):
        self.e.as_role("requirements", "accept", "R1", "AC1", "一覧に出る")
        self.e.as_role("requirements", "verify", "AC1", "V1", "単体テスト", "--level", "unit")
        self.e.as_role("requirements", "risk", "R1", "K1", "x", "--concurrency", "toctou")
        self.e.as_role("requirements", "verify", "K1", "V2", "競合のテスト", "--level", "integration")
        from helpers import SPEC_MD
        self.e.write_as("requirements", os.path.relpath(os.path.join(self.e.arts, "spec.md"), self.e.project), SPEC_MD)
        res = self.e.stop("requirements", "整理")  # 要求担当の終了時に、エンジンが Evidence Graph を確かめる
        self.assertTrue(blocked(res))
        for want in ("状態遷移の順序（--scenario）", "不変条件（--invariant）", "順序の決め方（verify ... --ordering）"):
            self.assertIn(want, res["reason"])
        self.e.as_role("requirements", "risk", "R1", "K1", "x", *K1)
        self.e.as_role("requirements", "verify", "K1", "V2", "競合のテスト", *V2)
        res = self.e.stop("requirements", "整理")
        self.assertNotIn("並行・共有", (res or {}).get("reason", ""))


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False, k1_args=K1, v2_args=V2)
        self.assertFalse(denied(self.e.spawn("reviewer")))
        self.brief = self.e.start("reviewer")["hookSpecificOutput"]["additionalContext"]

    def tearDown(self):
        self.e.cleanup()

    def stop(self, msg):
        return self.e.stop("reviewer", msg)

    def test_brief_lists_scenario_and_report_section(self):
        self.assertIn("## 並行処理・共有リソースのリスク", self.brief)
        self.assertIn("順序: A が存在を確認 → B が削除 → A が更新", self.brief)
        self.assertIn("### 競合シナリオ", self.brief)
        self.assertIn("ロックを一律に勧めず", self.brief)

    def test_section_is_required(self):
        res = self.stop(full("合格", 0, 0, []))
        self.assertTrue(blocked(res))
        self.assertIn("「### 競合シナリオ」の節がない", res["reason"])

    def test_problem_needs_fix_required_finding(self):
        res = self.stop(full("合格", 0, 0, [], scen("問題あり")))
        self.assertTrue(blocked(res))
        self.assertIn("修正必須の指摘がない", res["reason"])

    def test_concurrency_fix_needs_order_invariant_and_test(self):
        weak = FIX.split(" / 順序:")[0] + " / 提案: 条件付き更新にする"
        res = self.stop(full("要修正", 1, 0, [weak], scen("問題あり")))
        self.assertTrue(blocked(res))
        self.assertIn("順序、不変条件、再現テスト", res["reason"])

    def test_valid_reports(self):
        self.assertIsNone(self.stop(full("要修正", 1, 0, [FIX], scen("問題あり"))))
        rv = self.e.state()["reviews"][-1]
        self.assertTrue(rv["valid"])

    def test_needs_check_maps_to_missing_evidence(self):
        res = self.stop(full("合格", 0, 0, [], scen("要検証")))
        self.assertTrue(blocked(res))
        self.assertIn("要検証", res["reason"])
        self.assertIsNone(self.stop(full("要修正", 1, 0, [EVID], scen("要検証"))))

    def test_no_problem_passes(self):
        self.assertIsNone(self.stop(full("合格", 0, 0, [], scen("問題なし"))))
        self.assertTrue(self.e.state()["reviews"][-1]["passed"])


class ReviewFixesTest(unittest.TestCase):
    """独立レビューの指摘: 分類が指紋に入っていない・欄の値のパスで切れる。"""

    def test_removing_classification_after_approval_needs_reapproval(self):
        e = Env()
        try:
            build_to_review(e, with_question=False, k1_args=K1, v2_args=V2)
            e.spawn("requirements")
            e.start("requirements")
            e.as_role("requirements", "risk", "R1", "K1", "空のメモが保存される", "--concurrency", "")
            e.stop("requirements", "分類を外した")
            self.assertTrue(denied(e.spawn("reviewer")))
            self.assertIn("仕様", e.next())
        finally:
            e.cleanup()

    def test_adding_classification_makes_review_stale(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            g1 = __import__("aqdlc_core.state_model", fromlist=["x"]).graph_fp(e.state())
            st = e.state()
            st["requirements"][0]["risks"][0].update(concurrency=["toctou"], scenario="a", invariant="b")
            self.assertNotEqual(g1, __import__("aqdlc_core.state_model", fromlist=["x"]).graph_fp(st))
        finally:
            e.cleanup()

    def test_field_values_with_paths(self):
        from aqdlc_core import concurrency as CCm
        self.assertEqual(CCm._field("順序: /api/orders を A が読む → B が消す / 不変条件: x", "順序"),
                         "/api/orders を A が読む → B が消す")
        self.assertEqual(CCm._field("根拠: src/memo.py:3 / 提案: y", "根拠"), "src/memo.py:3")


class UnrelatedChangeTest(unittest.TestCase):
    """並行処理と関係のない run では、何も増えない。"""

    def test_nothing_added(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            self.assertFalse(denied(e.spawn("reviewer")))
            brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("競合シナリオ", brief)
            self.assertIsNone(e.stop("reviewer", full("合格", 0, 0, [])))
            self.assertNotIn("並行・共有", e.aq("next"))
        finally:
            e.cleanup()


class ExampleTestsTest(unittest.TestCase):
    def test_examples_pass(self):
        d = os.path.join(ROOT, "plugins", "aq-dlc", "templates", "examples", "concurrency")
        p = subprocess.run([sys.executable, "-B", "-m", "unittest", "test_concurrency_examples"], cwd=d,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Ran 6 tests", p.stderr)


if __name__ == "__main__":
    unittest.main()
