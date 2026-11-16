"""改善C: レビュー品質の記録と集計。

受け入れ条件
- 同じ指摘の重複を集計しない（明示の重複と、後のレビューでの再掲）
- 判定が終わっていなければ、確定値と暫定値を混ぜない（未判定は誤検知に数えない）
- 外部レビューの追加の指摘を記録し、内部レビューの改善の材料にできる（見逃しの原因・再発防止）
- 既存の記録を壊さず、少ないサンプルで集計を試せる。再現率（recall）は断定しない
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Env, build_to_review, denied  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import review_quality as RQ  # noqa: E402


def F(label, body, target="R1", sev=None, act="fix_required"):
    sev = sev or label[:2]
    return {"label": label, "severity": sev, "action_type": act,
            "text": f"区分: 実装 / 対象: {target} / 対応区分: 修正必須 / 内容: {body} / 根拠: x"}


SAMPLE = {
    "reviews": [
        {"n": 1, "seq": 10, "valid": True, "passed": False, "at": "t1",
         "findings": [F("重大1", "空のメモが保存される"), F("重大2", "認可の確認がない", "R2"),
                      F("軽微1", "変数名", sev="軽微", act="suggestion")]},
        {"n": 2, "seq": 20, "valid": False, "findings": [F("重大1", "無効なレビューの指摘")]},
        {"n": 3, "seq": 30, "valid": True, "passed": False, "at": "t3",
         "findings": [F("重大1", "認可の確認がない", "R2"), F("重大2", "ログに本文が出る", "R3")]},
        {"n": 4, "seq": 50, "valid": True, "passed": True, "at": "t4", "findings": []},
    ],
    "impl": {"rounds": [{"seq": 40, "fix": {"kind": "review", "ref": 3, "labels": ["重大1", "重大2"]}}]},
}


class SummaryTest(unittest.TestCase):
    def test_untriaged_is_provisional_and_not_false_positive(self):
        m = RQ.summarize(SAMPLE)
        self.assertEqual((m["detected_total"], m["detected_unique"], m["duplicates"]), (5, 4, 1))  # RV3.重大1 は再掲
        self.assertEqual((m["untriaged"], m["false_positive"], m["provisional"]), (4, 0, True))
        self.assertIsNone(m["precision"])

    def test_judgments_and_confirmed_values(self):
        s = copy.deepcopy(SAMPLE)
        s["finding_judgments"] = {"RV1.重大1": {"judgment": "valid"}, "RV1.重大2": {"judgment": "valid"},
                                  "RV1.軽微1": {"judgment": "false_positive"},
                                  "RV3.重大2": {"judgment": "accepted_risk"}}
        m = RQ.summarize(s)
        self.assertFalse(m["provisional"])
        self.assertEqual((m["valid"], m["accepted_risk"], m["false_positive"]), (2, 1, 1))
        self.assertEqual(m["precision"], 0.75)  # (妥当2 + 受容1) / (3 + 誤検知1)
        self.assertEqual(m["by_severity"]["軽微"]["valid_rate"], 0.0)
        self.assertEqual(m["by_severity"]["重大"]["valid_rate"], 1.0)

    def test_explicit_duplicate_excluded(self):
        s = copy.deepcopy(SAMPLE)
        s["finding_judgments"] = {"RV3.重大2": {"judgment": "duplicate", "of": "RV1.重大1"}}
        m = RQ.summarize(s)
        self.assertEqual((m["detected_unique"], m["duplicates"]), (3, 2))

    def test_inferred_fix_after_passed_review(self):
        rows = {r["key"]: r for r in RQ.rows(SAMPLE)}
        self.assertEqual((rows["RV3.重大2"]["resolution"], rows["RV3.重大2"]["resolution_inferred"]), ("fixed", True))
        self.assertIsNone(rows["RV1.重大1"]["resolution"])

    def test_external_findings_and_misses(self):
        s = copy.deepcopy(SAMPLE)
        s["finding_judgments"] = {"RV1.重大1": {"judgment": "valid"}, "EXT-1": {"judgment": "valid"}}
        s["external_findings"] = [
            {"id": "EXT-1", "source": "external_review", "severity": "high", "text": "TOCTOU", "missed": "yes"},
            {"id": "EXT-2", "source": "production", "severity": "low", "text": "未判定の外部の指摘"},
            {"id": "EXT-3", "source": "human", "severity": "medium", "text": "同じ", "same_as": "RV1.重大1",
             "judgment": "valid"}]
        m = RQ.summarize(s)
        self.assertEqual((m["external_additional"], m["confirmed_misses"]), (1, 1))  # EXT-2 は未判定、EXT-3 は内部と同じ
        self.assertEqual(m["observed_additional_rate"], 0.5)
        self.assertNotIn("recall", m)

    def test_legacy_state_without_any_records(self):
        m = RQ.summarize({"reviews": [{"n": 1, "seq": 1, "valid": True, "findings": [
            {"label": "重大1", "severity": "重大", "text": "古い形式（対応区分なし）"}]}]})
        self.assertEqual((m["detected_unique"], m["untriaged"]), (1, 1))

    def test_aggregate(self):
        a = RQ.summarize(SAMPLE)
        s = copy.deepcopy(SAMPLE)
        s["finding_judgments"] = {k: {"judgment": "valid"} for k in ("RV1.重大1", "RV1.重大2", "RV1.軽微1", "RV3.重大2")}
        b = RQ.summarize(s)
        tot = RQ.aggregate([("r1", a), ("r2", b)])
        self.assertEqual((tot["runs"], tot["valid"], tot["untriaged"], tot["provisional"]), (2, 4, 4, True))
        self.assertEqual(tot["precision"], 1.0)


class CommandTest(unittest.TestCase):
    def setUp(self):
        from test_review_actions import FIX, report
        self.e = Env()
        build_to_review(self.e, with_question=False)
        self.assertFalse(denied(self.e.spawn("reviewer")))
        self.e.start("reviewer")
        msg = report("要修正", 1, 0, [FIX]).replace("- AC1: 妥当\n", "- AC1: 妥当\n- AC2: 妥当\n- K1: 妥当\n")
        self.assertIsNone(self.e.stop("reviewer", msg))

    def tearDown(self):
        self.e.cleanup()

    def test_judge_resolve_and_list(self):
        out = self.e.aq("findings")
        self.assertIn("| RV1.重大1 | 内部レビュー | 重大 | 未判定 |", out)
        self.assertIn("暫定（未判定あり）", out)
        self.e.aq("finding-judge", "RV1.重大1", "valid", "--reason", "再現した")
        self.assertIn("修正済みには", self.e.aq("finding-resolve", "RV1.重大1", "fixed", ok=False))
        self.e.aq("finding-resolve", "RV1.重大1", "fixed", "--commit", "abc123", "--reason", "直した")
        out = self.e.aq("findings")
        self.assertIn("| RV1.重大1 | 内部レビュー | 重大 | 妥当・", out)
        self.assertIn("修正済み", out)
        self.assertIn("レビュー品質（確定）", out)
        self.assertIn("レビュー品質", self.e.aq("metrics"))

    def test_validation(self):
        self.assertIn("ありません", self.e.aq("finding-judge", "RV9.重大1", "valid", "--reason", "x", ok=False))
        self.assertIn("--of", self.e.aq("finding-judge", "RV1.重大1", "duplicate", "--reason", "x", ok=False))
        self.assertIn("--reason", self.e.aq("finding-judge", "RV1.重大1", "valid", ok=False))
        out = self.e.aq("finding-judge", "RV1.重大1", "valid", "--reason", "x", "--by", "human", ok=False)
        self.assertIn("人の返答に RV1.重大1 がありません", out)
        self.e.human_says("RV1.重大1 は妥当です")
        self.e.aq("finding-judge", "RV1.重大1", "valid", "--reason", "人が確認", "--by", "human")
        self.assertEqual(self.e.state()["finding_judgments"]["RV1.重大1"]["by"], "human")

    def test_external_finding(self):
        out = self.e.aq("finding-add", "削除の競合で参照が残る", "--source", "external_review", "--severity", "high",
                        "--missed", "yes", ok=False)
        self.assertIn("原因", out)
        self.e.aq("finding-add", "削除の競合で参照が残る", "--source", "external_review", "--severity", "high",
                  "--target", "R1", "--missed", "yes", "--cause", "TOCTOU の観点がなかった",
                  "--prevention", "risk に --concurrency toctou を付ける", "--judge", "valid")
        self.assertIn("api_key", self.e.aq("finding-add", "api_key=sk-abcdefghijklmnopqrstuvwxyz0123",
                                           "--source", "human", "--severity", "low", ok=False) + "api_key")
        out = self.e.aq("findings")
        self.assertIn("| EXT-1 | 外部レビュー・見逃し | high | 妥当", out)
        self.assertIn("確定した見逃し 1 件", out)
        self.assertIn("再現率ではない", out)

    def test_subagents_cannot_judge(self):
        res = self.e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {
            "command": f'python3 aqdlc.py finding-judge RV1.重大1 valid --project "{self.e.project}"'},
            "agent_type": "aq-dlc:reviewer", "agent_id": "x"})
        self.assertTrue(denied(res))

    def test_cross_run_view(self):
        out = self.e.aq("metrics", "--reviews")
        self.assertIn("| run | 指摘（重複を除く）", out)
        self.assertIn("再現率は出しません", out)


if __name__ == "__main__":
    unittest.main()
