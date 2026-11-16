"""レビューの指摘の対応区分（修正必須・証拠不足・改善提案）と判定。

- Phase 1: レビュー担当への指示。規則の文面が、引き継ぎ情報（agents.py）と agents/reviewer.md で食い違っていない
- REV-01: エンジンが対応区分を読み、ラベル・件数・判定との矛盾を受け付けない（判定マトリクス）。
  新しく提出される報告だけを厳しくし、保存済みのレビューの記録は従来どおり読める
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Env, blocked, build_to_review, denied  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import agents as A  # noqa: E402

HEAD = "## レビュー結果\n判定: {v}\n重大な指摘: {b}\n軽微な指摘: {m}\nレビュー対象: 仕様 x / 設計 y / コード z\n### 指摘\n"
TAIL = ("### 受け入れ条件ごとの検証の妥当性\n- AC1: 妥当\n### E2E の要否\n- 不要\n### 不足しているリスク\n- なし\n"
        "### 確認したこと\n- テストを実行\n")


def report(verdict, blocking, minor, findings):
    return HEAD.format(v=verdict, b=blocking, m=minor) + "\n".join(findings) + "\n" + TAIL


class ParseNewFormatTest(unittest.TestCase):
    def test_example_from_policy(self):
        """方針書の形式例（証拠不足の [重大] と、改善提案の [軽微]）。"""
        r, errs = A.parse_review(report("要修正", 1, 1, [
            "- [重大1] 区分: テスト / 対象: AC1, V2 / 対応区分: 証拠不足 / 内容: 他契約のデータを拒否する検証がない"
            " / 根拠: 検証V2とテストファイルの確認結果 / 提案: 他契約データの拒否を検証する",
            "- [軽微1] 区分: 実装 / 対象: R1 / 対応区分: 改善提案 / 内容: 重複処理を既存ユーティリティに寄せられる"
            " / 根拠: 該当ファイルの重複処理 / 提案: 任意の整理"]))
        self.assertEqual(errs, [])
        self.assertEqual((r["verdict"], r["blocking"], r["minor"], r["passed"]), ("要修正", 1, 1, False))
        self.assertEqual(r["findings"][0]["category"], "テスト")
        self.assertEqual(r["findings"][0]["targets"], ["AC1", "V2"])  # 対応区分を足しても対象の読み取りは変わらない
        self.assertIn("対応区分: 証拠不足", r["findings"][0]["text"])

    def test_improvement_only_can_pass(self):
        r, errs = A.parse_review(report("合格", 0, 1, [
            "- [軽微1] 区分: 実装 / 対象: R1 / 対応区分: 改善提案 / 内容: 名前を揃えられる / 提案: 任意"]))
        self.assertEqual(errs, [])
        self.assertTrue(r["passed"])

    def test_blocking_cannot_pass_whatever_the_action_type(self):
        """[重大] を「改善提案」と書いても、合格にはならない。"""
        r, errs = A.parse_review(report("合格", 1, 0, [
            "- [重大1] 区分: 実装 / 対象: R1 / 対応区分: 改善提案 / 内容: … / 提案: …"]))
        self.assertTrue(errs)
        self.assertFalse(r["passed"])

    def test_fix_required_without_reproduction_is_still_blocking(self):
        """コードで立証した認可の欠陥（再現は未実施）は、修正必須として要修正。"""
        r, errs = A.parse_review(report("要修正", 1, 0, [
            "- [重大1] 区分: 実装 / 対象: R2, AC2 / 対応区分: 修正必須 / 内容: 他の組織のメモを読める"
            " / 成立条件: org_id の異なる利用者が get_note を呼ぶ / 影響: 他の組織の情報が漏れる"
            " / 根拠: notes/service.py:52 の _visible が org_id を比べていない（再現は未実施） / 提案: org_id を比べる"]))
        self.assertEqual(errs, [])
        self.assertEqual((r["blocking"], r["passed"]), (1, False))

    def test_ambiguous_verdict_still_rejected(self):
        _r, errs = A.parse_review(report("合格 ではなく要修正", 0, 0, ["- なし"]))
        self.assertTrue(errs)

FIX = ("- [重大1] 区分: 実装 / 対象: R1, AC1 / 対応区分: 修正必須 / 内容: 空白だけのメモが保存される"
       " / 成立条件: 空白だけの文字列を渡す / 影響: 空のメモが一覧に出る / 根拠: src/memo.py:3 / 提案: strip して判定")
EVID = ("- [重大1] 区分: テスト / 対象: K1 / 対応区分: 証拠不足 / 内容: 空白だけのメモを拒否する検証がない"
        " / 根拠: K1 は重要な要求のリスク / 提案: 空白だけの入力の拒否を確かめるテストを足す")
SUGG = "- [軽微1] 区分: 実装 / 対象: R1 / 対応区分: 改善提案 / 内容: 変数名を揃えられる / 提案: 任意"


class DecisionMatrixTest(unittest.TestCase):
    """要件書 REV-01 の判定マトリクス（8パターン）。"""

    def check(self, verdict, b, m, findings, ok, passed=False, msg=None):
        r, errs = A.parse_review(report(verdict, b, m, findings))
        if ok:
            self.assertEqual(errs, [])
        else:
            self.assertTrue(errs)
            if msg:
                self.assertTrue(any(msg in e for e in errs), errs)
        self.assertEqual(r["passed"], passed)
        return r

    def test_1_fix_required(self):
        r = self.check("要修正", 1, 0, [FIX], ok=True)
        self.assertEqual(r["findings"][0]["action_type"], "fix_required")

    def test_2_missing_evidence(self):
        r = self.check("要修正", 1, 0, [EVID], ok=True)
        self.assertEqual(r["findings"][0]["action_type"], "missing_evidence")

    def test_3_suggestion_only_passes(self):
        r = self.check("合格", 0, 1, [SUGG], ok=True, passed=True)
        self.assertEqual(r["findings"][0]["action_type"], "suggestion")

    def test_4_suggestion_only_needs_fix_is_invalid(self):
        self.check("要修正", 0, 1, [SUGG], ok=False, msg="重大な指摘が0件")

    def test_5_fix_required_labelled_minor_is_invalid(self):
        f = FIX.replace("[重大1]", "[軽微1]")
        self.check("合格", 0, 1, [f], ok=False, msg="ラベルと矛盾")

    def test_6_suggestion_labelled_blocking_is_invalid(self):
        self.check("要修正", 1, 0, [SUGG.replace("[軽微1]", "[重大1]")], ok=False, msg="ラベルと矛盾")

    def test_7_missing_action_type_is_invalid(self):
        self.check("要修正", 1, 0, [FIX.replace(" / 対応区分: 修正必須", "")], ok=False, msg="対応区分がない")
        self.check("合格", 0, 1, [SUGG.replace(" / 対応区分: 改善提案", "")], ok=False, msg="対応区分がない")

    def test_8_duplicate_label_is_invalid(self):
        self.check("要修正", 2, 0, [FIX, EVID], ok=False, msg="重複")


class StructureTest(unittest.TestCase):
    def errs(self, verdict, b, m, findings):
        r, errs = A.parse_review(report(verdict, b, m, findings))
        self.assertFalse(r["passed"])
        return errs

    def test_unknown_and_multiple_action_types(self):
        self.assertTrue(any("不正" in e for e in self.errs("要修正", 1, 0, [FIX.replace("修正必須", "要対応")])))
        self.assertTrue(any("複数" in e for e in self.errs("要修正", 1, 0, [FIX.replace("修正必須", "修正必須|証拠不足")])))
        self.assertTrue(any("複数" in e for e in self.errs("要修正", 1, 0, [FIX + " / 対応区分: 証拠不足"])))

    def test_counts_must_match_both_ways(self):
        self.assertTrue(any("重大な指摘の数" in e for e in self.errs("要修正", 2, 0, [FIX])))
        self.assertTrue(any("軽微な指摘の数" in e for e in self.errs("要修正", 1, 2, [FIX, SUGG])))
        self.assertTrue(any("軽微な指摘: <数>" in e for e in self.errs(
            "要修正", 1, 0, [FIX]) + A.parse_review(report("要修正", 1, 0, [FIX]).replace("軽微な指摘: 0\n", ""))[1]))

    def test_numbering_gap(self):
        self.assertTrue(any("連番" in e for e in self.errs("要修正", 2, 0, [FIX, EVID.replace("[重大1]", "[重大3]")])))

    def test_required_fields(self):
        for k in ("成立条件", "影響", "根拠", "提案"):
            with self.subTest(k=k):
                f = __import__("re").sub(rf" / {k}: [^/]*", "", FIX)
                self.assertTrue(any(k in e and "必要な欄" in e for e in self.errs("要修正", 1, 0, [f])))
        f = FIX.replace("根拠: src/memo.py:3", "根拠: …")  # 空の欄（記号だけ）も欠落として扱う
        self.assertTrue(any("根拠" in e for e in self.errs("要修正", 1, 0, [f])))
        for k in ("内容", "根拠", "提案"):
            with self.subTest(k=k):
                f = __import__("re").sub(rf" / {k}: [^/]*", "", EVID)
                self.assertTrue(any(k in e and "必要な欄" in e for e in self.errs("要修正", 1, 0, [f])))

    def test_field_name_with_note_in_parentheses(self):
        f = FIX.replace("根拠: src/memo.py:3", "根拠（詳細は下の節）: src/memo.py:3")
        self.assertEqual(A.parse_review(report("要修正", 1, 0, [f]))[1], [])


class EngineFlowTest(unittest.TestCase):
    """エンジンを通した流れ: 矛盾した報告は書き直しを求め、直らなければ無効（修正ループに進めない）。"""

    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)
        self.assertFalse(denied(self.e.spawn("reviewer")))
        self.e.start("reviewer")

    def tearDown(self):
        self.e.cleanup()

    def full(self, verdict, b, m, findings):
        return report(verdict, b, m, findings).replace("- AC1: 妥当\n", "- AC1: 妥当\n- AC2: 妥当\n- K1: 妥当\n")

    def test_old_format_report_is_not_accepted(self):
        """新規の報告では、対応区分のない旧形式を受け付けない（書き直しを求める）。"""
        res = self.e.stop("reviewer", self.full("要修正", 1, 0, [FIX.replace(" / 対応区分: 修正必須", "")]))
        self.assertTrue(blocked(res))
        self.assertIn("対応区分がない", res["reason"])

    def test_invalid_after_retries_is_recorded_as_invalid_not_fix(self):
        msg = self.full("要修正", 1, 0, [SUGG.replace("[軽微1]", "[重大1]")])
        for _ in range(5):
            if not blocked(self.e.stop("reviewer", msg)):
                break
        rv = self.e.state()["reviews"][-1]
        self.assertFalse(rv["valid"])
        self.assertFalse(rv["passed"])
        nxt = self.e.next()
        self.assertIn("aq-dlc:reviewer", nxt)  # もう一度レビュー。実装の修正には進めない
        self.assertNotIn("aq-dlc:implementer", nxt)

    def test_evidence_target_must_name_ac_or_risk(self):
        res = self.e.stop("reviewer", self.full("要修正", 1, 0, [EVID.replace("対象: K1", "対象: tests/")]))
        self.assertTrue(blocked(res))
        self.assertIn("受け入れ条件・リスクの ID", res["reason"])

    def test_valid_report_records_action_type(self):
        self.assertIsNone(self.e.stop("reviewer", self.full("要修正", 1, 1, [EVID, SUGG])))
        rv = self.e.state()["reviews"][-1]
        self.assertTrue(rv["valid"])
        self.assertEqual([f["action_type"] for f in rv["findings"]], ["missing_evidence", "suggestion"])


class LegacyRecordTest(unittest.TestCase):
    """保存済みのレビューの記録（対応区分の値がない旧形式）を、従来どおり読んで再開できる。"""

    def test_old_record_without_action_type(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            e.spawn("reviewer")
            e.start("reviewer")
            full = report("要修正", 1, 0, [FIX]).replace("- AC1: 妥当\n", "- AC1: 妥当\n- AC2: 妥当\n- K1: 妥当\n")
            self.assertIsNone(e.stop("reviewer", full))
            st = e.state()
            for f in st["reviews"][-1]["findings"]:  # 旧形式の記録にする
                f.pop("action_type", None)
                f.pop("num", None)
                f["text"] = f["text"].replace(" / 対応区分: 修正必須", "")
            e.save_state(st)
            e.aq("status")
            self.assertIn("aq-dlc:implementer", e.next())  # 従来どおり修正ループへ
            e.spawn("implementer")
            brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
            self.assertIn("[重大1]", brief)
        finally:
            e.cleanup()


class RulesInBriefAndAgentTest(unittest.TestCase):
    def test_rules_match_reviewer_md(self):
        with open(os.path.join(ROOT, "plugins", "aq-dlc", "agents", "reviewer.md"), encoding="utf-8") as f:
            md = f.read()
        for line in A.REVIEW_ACTION_RULES:
            text = line.replace("#### ", "### ")
            self.assertIn(text, md, f"reviewer.md と引き継ぎ情報の規則が食い違う: {line[:40]}")
        self.assertIn("対応区分: 修正必須 /", md)
        self.assertIn("対応区分: 証拠不足 /", md)

    def test_brief_contains_rules(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            e.spawn("reviewer")
            brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
            for line in A.REVIEW_ACTION_RULES:
                self.assertIn(line, brief)
            self.assertIn("対応区分: 修正必須 /", brief)
            self.assertIn("対応区分: 証拠不足 /", brief)
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
