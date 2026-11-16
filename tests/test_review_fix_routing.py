"""REV-02: レビューの指摘を、対応区分ごとに修正依頼として渡す（既存の修正ループの中で。新しい担当・gate は作らない）。

AC-REV02-1 必須の検証の欠落だけなら、修正依頼は「検証の追加」で、「実装を書き換える」にならない
AC-REV02-2 実在する認可の欠陥は「実装の修正」に振り分ける
AC-REV02-3 改善提案しかないレビューは合格し、修正ループに入らない
AC-REV02-4 修正の後に、元の gate・レビューの結果を使って完了できない
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import MEMO_V2, Env, blocked, build_to_review, denied, implement  # noqa: E402
from test_review_actions import EVID, FIX, SUGG, report  # noqa: E402

AUTH = ("- [重大1] 区分: 実装 / 対象: R1, AC1 / 対応区分: 修正必須 / 内容: 他の利用者のメモを一覧で読める"
        " / 成立条件: 別の利用者が list を呼ぶ / 影響: 他人のメモが漏れる / 根拠: src/memo.py:5 が所有者を比べていない"
        " / 提案: 所有者で絞る")


def full(verdict, b, m, findings):
    return report(verdict, b, m, findings).replace("- AC1: 妥当\n", "- AC1: 妥当\n- AC2: 妥当\n- K1: 妥当\n")


class FixRoutingTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def review(self, msg):
        self.assertFalse(denied(self.e.spawn("reviewer")))
        self.e.start("reviewer")
        self.assertIsNone(self.e.stop("reviewer", msg))

    def impl_brief(self):
        self.assertFalse(denied(self.e.spawn("implementer")))
        return self.e.start("implementer")["hookSpecificOutput"]["additionalContext"]

    def test_evidence_only_asks_for_tests_not_code_changes(self):
        self.review(full("要修正", 1, 0, [EVID]))
        nxt = self.e.next()
        self.assertIn("検証の追加 1 件", nxt)
        self.assertIn("実装の修正 0 件", nxt)
        brief = self.impl_brief()
        self.assertIn("### 検証の追加（証拠不足）", brief)
        self.assertNotIn("### 実装の修正（修正必須）", brief)
        self.assertIn("根拠のないまま実装を書き換えない", brief)
        self.assertIn("完了条件: 対象の受け入れ条件・リスクを確かめるテスト", brief)
        fix = [v for v in self.e.state()["agents"]["running"].values() if v["role"] == "implementer"][0]["fix"]
        self.assertEqual(fix["actions"], {"重大1": "missing_evidence"})

    def test_authorization_defect_goes_to_code_fix(self):
        self.review(full("要修正", 1, 0, [AUTH]))
        brief = self.impl_brief()
        self.assertIn("### 実装の修正（修正必須）", brief)
        self.assertNotIn("### 検証の追加（証拠不足）", brief)
        self.assertIn("所有者で絞る", brief)
        self.assertIn("完了条件: 成立条件の入力・状態で期待どおりに動き", brief)

    def test_mixed_with_suggestion(self):
        evid2 = EVID.replace("[重大1]", "[重大2]")
        self.review(full("要修正", 2, 1, [FIX, evid2, SUGG]))
        brief = self.impl_brief()
        i_fix, i_ev, i_opt = (brief.index("### 実装の修正"), brief.index("### 検証の追加"),
                              brief.index("### 任意の改善"))
        self.assertLess(i_fix, i_ev)
        self.assertLess(i_ev, i_opt)
        self.assertIn("[軽微1]", brief[i_opt:])
        self.assertIn("テストの削除・skip・アサーションの緩和で合格させることは修正として認めない", brief)
        # 改善提案への対応は書かなくても終えられる（[重大1]・[重大2] は必須）
        implement(self.e, MEMO_V2, extra="## 指摘への対応\n- [重大1] strip して判定\n")
        res = self.e.stop("implementer", "修正")
        self.assertTrue(blocked(res))
        self.assertIn("重大2", res["reason"])
        self.assertNotIn("軽微1", res["reason"])
        implement(self.e, MEMO_V2, extra="## 指摘への対応\n- [重大1] strip して判定\n- [重大2] 拒否のテストを追加\n")
        self.assertIsNone(self.e.stop("implementer", "修正"))
        self.assertEqual(self.e.state()["impl"]["rounds"][-1]["fix"]["actions"],
                         {"重大1": "fix_required", "重大2": "missing_evidence"})

    def test_suggestion_only_passes_without_fix_loop(self):
        self.review(full("合格", 0, 1, [SUGG]))
        rv = self.e.state()["reviews"][-1]
        self.assertTrue(rv["passed"])
        self.assertNotIn("aq-dlc:implementer", self.e.next())
        self.assertTrue(denied(self.e.spawn("implementer")) or "修正依頼" not in self.impl_brief())

    def test_after_fix_old_gate_and_review_are_not_reused(self):
        self.review(full("要修正", 1, 0, [EVID]))
        self.impl_brief()
        implement(self.e, MEMO_V2, extra="## 指摘への対応\n- [重大1] 拒否のテストを追加\n")
        self.assertIsNone(self.e.stop("implementer", "修正"))
        self.assertIn("gate 後に変わったものがある", self.e.next())
        self.assertTrue(denied(self.e.spawn("reviewer")))  # gate の前はレビューできない
        self.e.aq("complete", ok=False)

    def test_legacy_record_without_action_type(self):
        self.review(full("要修正", 1, 0, [FIX]))
        st = self.e.state()
        for f in st["reviews"][-1]["findings"]:
            f.pop("action_type", None)
        self.e.save_state(st)
        nxt = self.e.next()
        self.assertNotIn("検証の追加", nxt)  # 内訳は、対応区分の記録があるときだけ
        brief = self.impl_brief()
        self.assertIn("### 指摘（対応区分の記録なし）", brief)
        self.assertIn("[重大1]", brief)


if __name__ == "__main__":
    unittest.main()
