"""リスクに応じた工程の選択（workflow.mode）。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import MEMO_V1, SPEC_MD, TEST_MEMO, Env, denied, edit_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import workflow as WF  # noqa: E402

IMPL_NO_DESIGN = """# 実装報告
仕様: {spec}
## 実施内容
既存の構成に合わせて src/memo.py に実装した（標準ライブラリのみ）。
## 検証結果
python3 -m unittest で合格。
## 差し戻し
- なし
"""


def spec_approved(e, mode="auto", reqs=None, extra_config=None):
    e.aq("init", "メモ")
    cfg = {"workflow": {"mode": mode}}
    cfg.update(extra_config or {})
    edit_json(os.path.join(e.project, ".aq-dlc", "config.json"), lambda c: c.update(cfg))
    e.aq("new", "メモ")
    e.spawn("requirements")
    e.start("requirements")
    for args in reqs or [("req", "R1", "メモを追加できる"), ("req", "R2", "1秒以内に応答する"),
                         ("accept", "R1", "AC1", "追加したメモが一覧に出る"), ("accept", "R2", "AC2", "応答が1秒以内"),
                         ("risk", "R1", "K1", "空のメモが保存される"),
                         ("verify", "AC1", "V1", "追加の単体テスト", "--level", "unit"),
                         ("verify", "K1", "V2", "空のメモを拒否する単体テスト", "--level", "unit"),
                         ("verify", "AC2", "V3", "全テストが通る", "--level", "unit")]:
        e.as_role("requirements", *args)
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project), SPEC_MD)
    assert e.stop("requirements", "要求を整理") is None
    out = e.approve_spec()
    return out


def implement_without_design(e, impl_files=("src/memo.py",)):
    py = f'"{sys.executable}"'
    e.write_as("implementer", "src/memo.py", MEMO_V1)
    e.write_as("implementer", "tests/test_memo.py", TEST_MEMO)
    for f in impl_files:
        if not os.path.exists(os.path.join(e.project, f)):
            e.write_as("implementer", f, "X = 1\n")
    e.as_role("implementer", "add-check", "unit", f"{py} -m unittest discover -s tests -q", "--kind", "unit")
    for r in ("R1", "R2"):
        e.as_role("implementer", "impl-map", r, "--files", ",".join(impl_files))
    e.as_role("implementer", "bind", "V1", "--command", f"{py} -m unittest tests.test_memo.T.test_add")
    e.as_role("implementer", "bind", "V2", "--command", f"{py} -m unittest tests.test_memo.T.test_empty")
    e.as_role("implementer", "bind", "V3", "--checks", "unit")
    e.write_as("implementer", os.path.relpath(os.path.join(e.arts, "impl.md"), e.project),
               IMPL_NO_DESIGN.format(spec=e.state()["spec"]["registered_hash"][:8]))


class ClassifyTest(unittest.TestCase):
    def st(self, texts, critical=False):
        reqs = [{"id": f"R{i}", "text": t, "critical": critical, "acceptance": [], "risks": []}
                for i, t in enumerate(texts, 1)]
        return {"requirements": reqs}

    def test_rules(self):
        self.assertEqual(WF.classify({"requirements": []})[0], "strict")  # 情報がなければ安全側
        self.assertEqual(WF.classify(self.st(["メモを追加できる"]))[0], "lite")
        # 「重要」の印では工程を上げない（主な要求にはほぼ必ず付くため。実験11）
        self.assertEqual(WF.classify(self.st(["メモを追加できる"], critical=True))[0], "lite")
        self.assertEqual(WF.classify(self.st(["a", "b", "c"]))[0], "lite")
        self.assertEqual(WF.classify(self.st(["a", "b", "c", "d"]))[0], "standard")
        for t in ("ログインできる", "管理者だけが削除できる", "テーブルにマイグレーションを追加", "同時に更新しても壊れない",
                  "決済を行う", "OAuth で認証する"):
            self.assertEqual(WF.classify(self.st([t]))[0], "strict", t)

    def test_default_full_is_strict(self):
        e = WF.effective({"requirements": []}, {"workflow": {"mode": "full"}})
        self.assertEqual((e["mode"], e["stages"]), ("strict", {"design": True, "review": True}))
        self.assertEqual(WF.config_mode({}), "full")


class WorkflowFlowTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_lite_skips_design_and_review_without_claiming_them(self):
        e = self.e
        out = spec_approved(e)
        self.assertIn("lite", out)
        self.assertTrue(denied(e.spawn("design")))  # この工程では設計担当を使わない
        self.assertFalse(denied(e.spawn("implementer")))
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("設計担当はいない", brief)
        self.assertIn("add-check", brief)
        implement_without_design(e)
        self.assertIsNone(e.stop("implementer", "実装完了"))
        e.aq("gate")
        self.assertTrue(denied(e.spawn("reviewer")))  # 独立レビューも使わない
        nxt = e.next()
        self.assertNotIn("レビュー", nxt.split("\n")[1])
        self.assertIn("Memory", nxt)
        ev = e.aq("evidence")
        self.assertIn("省略（工程の選択による）", ev)  # レビュー済みとは書かない
        self.assertIn("省略", e.aq("report"))
        e.aq("memory-done", "--end", "project", "--skipped", "テスト用")
        e.aq("complete")
        self.assertEqual(e.state()["status"], "complete")

    def test_strict_words_require_design(self):
        e = self.e
        spec_approved(e, reqs=[("req", "R1", "ログインしたユーザーだけがメモを追加できる"),
                               ("accept", "R1", "AC1", "未ログインは拒否される"),
                               ("verify", "AC1", "V1", "拒否の単体テスト", "--level", "unit")])
        self.assertIn("strict", e.aq("workflow"))
        self.assertFalse(denied(e.spawn("design")))
        self.assertIn("設計", e.next())

    def test_auth_file_upgrades_to_strict(self):
        e = self.e
        spec_approved(e)
        e.spawn("implementer")
        e.start("implementer")
        implement_without_design(e, impl_files=("src/memo.py", "src/auth/session.py"))
        e.stop("implementer", "実装完了")
        self.assertIn("strict", e.aq("workflow"))  # 実装の対象ファイルで上げた
        self.assertIn("設計が未作成", e.next())  # 上がった工程の欠けている成果物を求める

    def test_lowering_needs_human_approval_raising_does_not(self):
        e = self.e
        spec_approved(e)
        e.aq("workflow", "--set", "standard", "人間が慎重に進めたいと言った")
        self.assertIn("standard", e.aq("workflow"))
        e.aq("workflow", "--set", "lite", "下げる", ok=False)  # 承認なし
        e.aq("ask", "工程を lite に下げますか")
        e.human_says("はい")
        e.aq("workflow", "--set", "lite", "下げる", ok=False)  # 対象のない返答は承認にしない
        out = e.aq("ask", "工程")
        t = WF.mode_target(e.state(), "lite")
        e.human_says(f"承認 {t}")
        e.aq("workflow", "--set", "lite", "人間が承認")
        self.assertIn("工程: lite", e.aq("workflow"))
        # サブエージェントは工程を変えられない
        res = e.as_role("implementer", "workflow", "--set", "strict", "x", ok=None)
        self.assertTrue((isinstance(res, dict) and denied(res)) or "統括だけ" in str(res))
        self.assertIn("工程: lite", e.aq("workflow"))

    def test_full_config_cannot_change(self):
        e = self.e
        spec_approved(e, mode="full")
        self.assertFalse(denied(e.spawn("design")))
        e.aq("workflow", "--set", "lite", "x", ok=False)

    def test_holdout_keeps_design(self):
        e = self.e
        spec_approved(e, extra_config={"holdout": {"enabled": True}})
        self.assertFalse(denied(e.spawn("design")))  # 隠し受け入れテストを書く設計担当は省かない
        self.assertIn("隠し受け入れテスト", e.aq("workflow"))


if __name__ == "__main__":
    unittest.main()
