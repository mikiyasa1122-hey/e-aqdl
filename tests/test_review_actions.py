"""レビューの指摘の対応区分（修正必須・証拠不足・改善提案）と判定（Phase 1: レビュー担当への指示）。

Phase 1 ではパーサーを変えない。ここで確かめること:
- 対応区分を書いた新しい形式の報告を、既存のパーサーが従来どおり読める
- 改善提案（[軽微]）だけなら合格できる。[重大] があれば対応区分にかかわらず合格できない（既存の挙動のまま）
- 規則の文面が、引き継ぎ情報（agents.py）と agents/reviewer.md で食い違っていない
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Env, build_to_review  # noqa: E402

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
        """[重大] を「改善提案」と書いても、合格にはならない（既存の矛盾の検出のまま）。"""
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


class RulesInBriefAndAgentTest(unittest.TestCase):
    def test_rules_match_reviewer_md(self):
        with open(os.path.join(ROOT, "plugins", "aq-dlc", "agents", "reviewer.md"), encoding="utf-8") as f:
            md = f.read()
        for line in A.REVIEW_ACTION_RULES:
            text = line.replace("#### ", "### ")
            self.assertIn(text, md, f"reviewer.md と引き継ぎ情報の規則が食い違う: {line[:40]}")
        self.assertIn("対応区分: 修正必須|証拠不足", md)

    def test_brief_contains_rules(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            e.spawn("reviewer")
            brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
            for line in A.REVIEW_ACTION_RULES:
                self.assertIn(line, brief)
            self.assertIn("対応区分: 修正必須|証拠不足", brief)
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
