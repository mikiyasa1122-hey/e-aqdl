"""判断シート: 推奨のある質問と人の確認を、仕様の承認と同じ1回の返答で決める。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, SPEC_MD, Env, denied  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import decision_sheet as DS  # noqa: E402


def requirements_with(e, question_options="制限なし（推奨）|100文字", manual=False):
    e.aq("init", "メモ")
    e.aq("new", "メモ")
    e.spawn("requirements")
    e.start("requirements")
    e.as_role("requirements", "req", "R1", "メモを追加できる", "--critical")
    e.as_role("requirements", "accept", "R1", "AC1", "追加したメモが一覧に出る")
    e.as_role("requirements", "risk", "R1", "K1", "空のメモが保存される")
    e.as_role("requirements", "verify", "AC1", "V1", "追加の単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "K1", "V2", "空のメモを拒否する単体テスト", "--level", "unit")
    if manual:
        e.as_role("requirements", "verify", "AC1", "V8", "本番の画面で見え方を確かめる", "--manual", "本番で一覧を開く")
    e.as_role("requirements", "question", "Q1", "メモの最大文字数は？", "--options", question_options)
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project), SPEC_MD)
    assert e.stop("requirements", "要求を整理") is None


class DecisionSheetTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def present(self):
        out = self.e.aq("spec", "--present")
        return out, re.search(r"承認 (SPEC-[0-9a-f]{8})", out).group(1)

    def test_recommended_question_is_decided_by_approval(self):
        e = self.e
        requirements_with(e)
        self.assertNotIn("未回答の質問", e.next())  # 推奨のある質問は、提示を止めない
        out, t = self.present()
        self.assertIn("判断シート", out)
        self.assertIn("推奨: 制限なし", out)
        e.human_says(f"承認 {t}")
        e.aq("spec", "--approve", "推奨のまま承認")
        q = e.state()["questions"][0]
        self.assertEqual((q["status"], q["answer"], q["answered_by"]), ("answered", "制限なし", "spec_approval"))
        self.assertNotIn("未反映", e.next())  # 仕様は推奨を前提に書かれているので、反映済み
        self.assertFalse(denied(e.spawn("design")))

    def test_reply_that_names_a_question_is_not_approval(self):
        e = self.e
        requirements_with(e)
        _out, t = self.present()
        e.human_says(f"承認 {t}。ただし Q1 は 100文字で")
        e.aq("spec", "--approve", "x", ok=False)
        self.assertEqual(e.state()["questions"][0]["status"], "open")
        self.assertIsNone(e.state()["spec"].get("approved_hash"))

    def test_question_without_recommendation_is_asked_first(self):
        e = self.e
        requirements_with(e, question_options="制限なし|100文字")
        self.assertIn("推奨の選択肢なし", e.next())
        e.aq("spec", "--present", ok=False)

    def test_manual_verification_can_be_deferred_in_the_same_reply(self):
        e = self.e
        requirements_with(e, manual=True)
        out, t = self.present()
        self.assertIn("承認 DEFER-V8", out)  # 人の確認も同じシートで決められる
        e.human_says(f"承認 {t}\n承認 DEFER-V8")
        e.aq("spec", "--approve", "承認と V8 は確認しないで進める")
        e.aq("defer", "V8", "本番を確かめられないため、未確認のまま進める")
        self.assertTrue(e.state()["requirements"][0]["acceptance"][0]["verifications"][1].get("deferred"))
        self.assertNotIn("承認 DEFER-V8", e.aq("spec"))  # 決めた項目はシートから消える


    # ---- 推奨の厳密な解析（「（推奨）」の印だけを推奨とみなす）
    def test_not_recommended_marker_is_not_recommendation(self):
        e = self.e
        requirements_with(e, question_options="非推奨の案|100文字")
        self.assertIn("推奨の選択肢なし", e.next())
        e.aq("spec", "--present", ok=False)

    def test_word_in_body_is_not_marker(self):
        e = self.e
        requirements_with(e, question_options="推奨値を使う|100文字")
        self.assertIn("推奨の選択肢なし", e.next())

    def test_half_width_marker(self):
        e = self.e
        requirements_with(e, question_options="制限なし (推奨)|100文字")
        out, _t = self.present()
        self.assertIn("推奨: 制限なし", out)

    def test_multiple_recommendations_rejected(self):
        e = self.e
        requirements_with(e)
        e.as_role("requirements", "question", "Q2", "並び順は？", "--options", "新しい順（推奨）|古い順（推奨）",
                  ok=False)

    # ---- 提示した推奨と、承認で決まる推奨が一致する
    def test_changing_recommendation_after_presentation_needs_new_presentation(self):
        e = self.e
        requirements_with(e)
        _out, t = self.present()
        # 提示の後に、要求分析担当が推奨を変えた
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "question", "Q1", "メモの最大文字数は？", "--options", "100文字（推奨）|制限なし")
        assert e.stop("requirements", "推奨を見直した") is None
        e.human_says(f"承認 {t}")
        e.aq("spec", "--approve", "x", ok=False)  # 見せていない推奨では決めない
        self.assertEqual(e.state()["questions"][0]["status"], "open")
        # 提示し直して承認すれば、新しい推奨で決まる
        out, t2 = self.present()
        self.assertNotEqual(t, t2)
        self.assertIn("推奨: 100文字", out)
        e.human_says(f"承認 {t2}")
        e.aq("spec", "--approve", "新しい推奨で承認")
        self.assertEqual(e.state()["questions"][0]["answer"], "100文字")
        # 承認の前後で仕様の指紋は変わらない（承認しただけで仕様が古くならない）
        self.assertNotIn("仕様が承認後に変更", e.next())
        self.assertFalse(denied(e.spawn("design")))

    # ---- 推奨どおりと言っているだけの返答は承認として扱う（実験14）
    def test_reply_confirming_recommendations_by_id_is_approval(self):
        e = self.e
        requirements_with(e)
        _out, t = self.present()
        e.human_says(f"食い違いはありません。Q1 は推奨案でお願いします。\n\n承認 {t}")
        e.aq("spec", "--approve", "推奨のまま承認")
        q = e.state()["questions"][0]
        self.assertEqual((q["status"], q["answer"]), ("answered", "制限なし"))

    def test_reply_with_range_of_ids_and_recommendation_is_approval(self):
        e = self.e
        requirements_with(e)
        _out, t = self.present()
        e.human_says(f"Q1〜Q4 の推奨どおりで問題ありません。\nこの仕様で進める（承認 {t}）")
        e.aq("spec", "--approve", "推奨のまま承認")
        self.assertEqual(e.state()["questions"][0]["status"], "answered")

    def test_reply_choosing_other_option_is_refused(self):
        e = self.e
        requirements_with(e)
        _out, t = self.present()
        e.human_says(f"Q1 は推奨ではなく 100文字 にしてください。承認 {t}")
        e.aq("spec", "--approve", "x", ok=False)
        e.human_says(f"Q1 は推奨以外で。承認 {t}")
        e.aq("spec", "--approve", "x", ok=False)
        e.human_says(f"食い違いはありません。ただ Q1 は変えたいです。承認 {t}")
        e.aq("spec", "--approve", "x", ok=False)
        self.assertIsNone(e.state()["spec"].get("approved_hash"))


class RulesTest(unittest.TestCase):
    """判断シートの規則（外部コマンドなしで確かめる）。"""
    Q = [{"id": "Q1", "options": ["制限なし（推奨）", "100文字"]}, {"id": "Q2", "options": ["新しい順(推奨)", "古い順"]}]

    def test_recommended_option(self):
        self.assertEqual(DS.recommended_option({"options": ["A（推奨）", "B"]}), "A")
        self.assertEqual(DS.recommended_option({"options": ["A ( 推奨 )", "B"]}), "A")
        for opts in (["非推奨の案", "B"], ["推奨値を使う", "B"], ["A（推奨）", "B（推奨）"], ["（推奨）", "B"], [], ["A"]):
            self.assertIsNone(DS.recommended_option({"options": opts}), opts)
        self.assertIsNotNone(DS.options_error(["A（推奨）", "B（推奨）"]))
        self.assertIsNotNone(DS.options_error(["（推奨）", "B"]))
        self.assertIsNone(DS.options_error(["A（推奨）", "非推奨の B"]))

    def test_reply_conflicts(self):
        ok = ["承認 SPEC-1", "食い違いはありません。Q1 は推奨案でお願いします。承認 SPEC-1",
              "意図と食い違う点はありません。Q1〜Q2 は推奨案で。", "Q1は推奨で。変更は不要です", "Q1 は制限なしで"]
        ng = ["Q1 は推奨、Q2 は古い順", "Q2 は別の案", "Q1 は B で", "承認。文字数は100文字で", "Q1 は推奨ではなく",
              "食い違いはありませんが Q1 は変えたい"]
        for t in ok:
            self.assertEqual(DS.reply_conflicts(t, self.Q), [], t)
        for t in ng:
            self.assertTrue(DS.reply_conflicts(t, self.Q), t)

    def test_fingerprint_items_stable_across_approval(self):
        before = [{"id": "Q1", "text": "t", "options": ["A（推奨）", "B"], "status": "open"}]
        after = [{"id": "Q1", "text": "t", "options": ["A（推奨）", "B"], "status": "answered", "answer": "A",
                  "answered_by": "spec_approval"}]
        self.assertEqual(DS.fingerprint_items(before), DS.fingerprint_items(after))
        self.assertEqual(DS.fingerprint_items([{"id": "Q1", "options": ["A", "B"], "status": "open"}]), [])


if __name__ == "__main__":
    unittest.main()
