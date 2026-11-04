"""判断シート: 推奨のある質問と人の確認を、仕様の承認と同じ1回の返答で決める。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import SPEC_MD, Env, denied  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
