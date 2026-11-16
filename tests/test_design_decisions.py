"""改善D: 設計方針の選択（DEC）を、仕様策定の正式な工程にする。

受け入れ条件
- 重要な設計判断を、2〜3案の比較と推奨の理由つきで登録できる（数合わせ・現状踏襲の検討の欠落は拒否）
- 人が「DEC-001 = B」のように選ぶと、仕様の記録に残り、その方針で後続工程が進む（書かなければ推奨）
- 未決定の重要な論点が残っていれば、依存する設計・実装・レビューを開始しない
- 選択を変えると、設計・実装（とその後の gate・レビュー）が更新の対象になる
- 単純な修正では、選択の質問を大量に出さない（未決定は同時に3件まで）
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import DESIGN_MD, MEMO_V1, ROOT, SPEC_MD, Env, denied, implement  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import design_decisions as DD  # noqa: E402

OPT_A = "A|条件付き更新＋競合時エラー|実装が単純|呼び出し側に再試行が要る|単一DBで競合が少ない"
OPT_B = "B|行ロックで直列化|更新順を制御しやすい|ロック待ち・デッドロックの考慮|同じ行への競合が多い"
OPT_C = "C|キュー経由で順序制御|複数処理を集約できる|運用と再試行が複雑|非同期の順序保証が必要"


def dec_args(did="DEC-001", *extra):
    return ["dec", did, "更新の整合性をどう保証するか", "--option", OPT_A, "--option", OPT_B, "--option", OPT_C,
            "--recommend", "A", "--affects", "R1", "--minimal", "A", "--assume", "単一DB・低競合", *extra]


def requirements_with_dec(e, register=True):
    e.aq("init", "メモ")
    e.aq("new", "メモ")
    e.aq("memory-scope", "project")
    e.spawn("requirements")
    e.start("requirements")
    e.as_role("requirements", "req", "R1", "メモを追加できる", "--critical")
    e.as_role("requirements", "req", "R2", "1秒以内に応答する", "--kind", "nonfunctional")
    e.as_role("requirements", "accept", "R1", "AC1", "追加したメモが一覧に出る")
    e.as_role("requirements", "accept", "R2", "AC2", "応答が1秒以内")
    e.as_role("requirements", "risk", "R1", "K1", "空のメモが保存される")
    e.as_role("requirements", "verify", "AC1", "V1", "追加の単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "K1", "V2", "空のメモを拒否する単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "AC2", "V3", "全テストが通る", "--level", "unit")
    if register:
        e.as_role("requirements", *dec_args())
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project), SPEC_MD)
    assert e.stop("requirements", "要求を整理") is None


def present(e):
    out = e.aq("spec", "--present")
    return out, re.search(r"承認 (SPEC-[0-9a-f]{8})", out).group(1)


def design_md(e, ids=()):
    """ids: 書く DEC の ID。選ばれている案を「DEC-001 = A」の形で書く。"""
    from_spec = e.state()["spec"]["registered_hash"]
    body = DESIGN_MD.format(spec=from_spec[:8])
    sel = {d["id"]: d.get("selected") for d in e.state().get("design_decisions", [])}
    if ids:
        body = body.replace("## 設計判断とトレードオフ", "## 設計判断とトレードオフ\n"
                            + "\n".join(f"- {i} = {sel.get(i)}: 採用した案。採用しない案の理由も書く" for i in ids))
    return body


def run_design(e, ids=("DEC-001",)):
    py = f'"{sys.executable}"'
    assert not denied(e.spawn("design"))
    e.start("design")
    e.as_role("design", "design-map", "R1", "src/memo.py の add")
    e.as_role("design", "design-map", "R2", "src/memo.py（計算量 O(1)）")
    e.as_role("design", "add-check", "unit", f"{py} -m unittest discover -s tests -q", "--kind", "unit", ok=None)
    e.write_as("design", os.path.relpath(os.path.join(e.arts, "design.md"), e.project), design_md(e, ids))
    return e.stop("design", "設計完了")


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

    def reject(self, args, msg):
        out = self.e.as_role("requirements", *args, ok=False)
        self.assertIn(msg, out)

    def test_valid_registration(self):
        self.e.as_role("requirements", *dec_args())
        d = self.e.state()["design_decisions"][0]
        self.assertEqual((d["status"], d["recommended"], d["origin"], [o["key"] for o in d["options"]]),
                         ("proposed", "A", "requirements", ["A", "B", "C"]))
        self.assertEqual(d["options"][1]["cons"], "ロック待ち・デッドロックの考慮")

    def test_needs_two_or_three_options(self):
        self.reject(["dec", "DEC-001", "論点", "--option", OPT_A, "--recommend", "A", "--affects", "R1",
                     "--minimal", "A"], "2〜3案")

    def test_needs_minimal_change_consideration(self):
        args = dec_args()[:]
        i = args.index("--minimal")
        del args[i:i + 2]
        self.reject(args, "現状踏襲・最小変更")
        self.e.as_role("requirements", *(args + ["--minimal-rejected", "現状の構成では整合性を保証できない"]))

    def test_fields_and_references(self):
        self.reject(["dec", "DEC-001", "論点", "--option", "A|方針||欠点|条件", "--option", OPT_B,
                     "--recommend", "A", "--affects", "R1", "--minimal", "A"], "欄が空")
        a = dec_args()
        a[a.index("--affects") + 1] = "R1,R9"
        self.reject(a, "存在しない要求")
        self.reject(dec_args("D1"), "DEC-001 の形")
        a = dec_args()
        a[a.index("--recommend") + 1] = "D"
        self.reject(a, "推奨")

    def test_open_limit(self):
        for i in (1, 2, 3):
            self.e.as_role("requirements", *dec_args(f"DEC-00{i}"))
        self.reject(dec_args("DEC-004"), "3 件")

    def test_orchestrator_cannot_register(self):
        out = self.e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {
            "command": f'python3 aqdlc.py dec DEC-001 x --project "{self.e.project}"'}})
        self.assertTrue(denied(out))


class SpecApprovalTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        requirements_with_dec(self.e)

    def tearDown(self):
        self.e.cleanup()

    def test_sheet_shows_table_and_choice_prompt(self):
        out, _t = present(self.e)
        self.assertIn("## 設計方針の選択", out)
        self.assertIn("| A（推奨）（現状踏襲・最小変更） | 条件付き更新＋競合時エラー |", out)
        self.assertIn("確認したい選択: DEC-001 = A / B / C", out)

    def test_approval_without_mention_selects_recommended(self):
        _out, t = present(self.e)
        self.e.human_says(f"承認 {t}")
        self.e.aq("spec", "--approve", "承認")
        d = self.e.state()["design_decisions"][0]
        self.assertEqual((d["status"], d["selected"], d["decided_by"]), ("selected", "A", "spec_approval"))

    def test_explicit_choice_is_recorded(self):
        _out, t = present(self.e)
        self.e.human_says(f"承認 {t}\nDEC-001 = B")
        out = self.e.aq("spec", "--approve", "承認・DEC-001 は B")
        d = self.e.state()["design_decisions"][0]
        self.assertEqual((d["selected"], d["decided_by"]), ("B", "human"))
        self.assertIn("DEC-001 = B（人の選択）", out)
        self.assertEqual(self.e.state()["spec"]["approved_hash"], self.e.state()["spec"]["registered_hash"])
        self.assertIn("採用しない: A", self.e.aq("decisions"))

    def test_ambiguous_choice_is_not_approval(self):
        _out, t = present(self.e)
        self.e.human_says(f"承認 {t}\nDEC-001 は B ではなく C")
        out = self.e.aq("spec", "--approve", "承認", ok=False)
        self.assertIn("曖昧", out)
        self.assertIsNone(self.e.state()["spec"].get("approved_hash"))
        self.assertEqual(self.e.state()["design_decisions"][0]["status"], "proposed")

    def test_changing_dec_after_present_requires_new_presentation(self):
        _out, t = present(self.e)
        self.e.spawn("requirements")
        self.e.start("requirements")
        a = dec_args()
        a[a.index("--recommend") + 1] = "B"
        self.e.as_role("requirements", *a)
        assert self.e.stop("requirements", "推奨を変更") is None
        self.e.human_says(f"承認 {t}")
        self.e.aq("spec", "--approve", "承認", ok=False)

    def test_spec_hash_unchanged_without_decisions(self):
        e2 = Env()
        try:
            requirements_with_dec(e2, register=False)
            st = e2.state()
            from aqdlc_core.state_store import spec_hash
            h1 = spec_hash(st, e2.run_dir)
            st.pop("design_decisions", None)
            self.assertEqual(spec_hash(st, e2.run_dir), h1)
        finally:
            e2.cleanup()


class FlowTest(unittest.TestCase):
    """仕様の承認で決まった後: 設計・実装の制約になり、再確認・選び直しで後続が古くなる。"""

    def setUp(self):
        self.e = Env()
        requirements_with_dec(self.e)
        _out, t = present(self.e)
        self.e.human_says(f"承認 {t}")
        self.e.aq("spec", "--approve", "承認")

    def tearDown(self):
        self.e.cleanup()

    def test_design_must_cite_adopted_decision(self):
        res = run_design(self.e, ids=())
        self.assertIsNotNone(res)
        self.assertIn("DEC-001 = A", res["reason"])

    def test_briefs_carry_the_constraint(self):
        self.assertFalse(denied(self.e.spawn("design")))
        brief = self.e.start("design")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("DEC-001 更新の整合性をどう保証するか → **A: 条件付き更新＋競合時エラー**", brief)
        self.assertIn("採用しない: B: 行ロックで直列化", brief)
        self.assertIn("dec-revisit", brief)

    def test_new_design_decision_blocks_until_selected(self):
        self.assertIsNone(run_design(self.e))
        self.e.spawn("design")
        self.e.start("design")
        self.e.as_role("design", "dec", "DEC-002", "保存形式をどうするか",
                       "--option", "A|SQLite のまま|変更が小さい|検索が遅い|件数が少ない",
                       "--option", "B|全文検索の索引を足す|検索が速い|移行が要る|件数が多い",
                       "--recommend", "A", "--affects", "R1", "--minimal", "A")
        self.assertEqual(self.e.state()["design_decisions"][1]["origin"], "design")
        self.e.stop("design", "論点を追加")
        nxt = self.e.next()
        self.assertIn("未決定の設計方針", nxt)
        self.assertTrue(denied(self.e.spawn("implementer")))
        self.assertIn("A / B", self.e.aq("decisions", "--pending"))
        # 返答がないうちは記録できない
        self.assertIn("選択（「DEC-002 = B」）がありません", self.e.aq("dec-select", "DEC-002", "B", ok=False))
        self.e.human_says("DEC-002 = B")
        self.assertIn("返答どおり", self.e.aq("dec-select", "DEC-002", "A", ok=False))
        self.e.aq("dec-select", "DEC-002", "B", "--reason", "件数が多い見込み")
        self.assertIn("設計方針（DEC）の選択が変わった後に、設計が更新されていない", self.e.next())
        self.assertIsNone(run_design(self.e, ids=("DEC-001", "DEC-002")))
        self.assertFalse(denied(self.e.spawn("implementer")))

    def test_revisit_and_reselect_makes_design_and_impl_stale(self):
        self.assertIsNone(run_design(self.e))
        self.assertFalse(denied(self.e.spawn("implementer")))
        self.e.start("implementer")
        implement(self.e, MEMO_V1)
        self.assertIsNone(self.e.stop("implementer", "実装"))
        self.e.spawn("implementer")
        self.e.start("implementer")
        self.e.as_role("implementer", "dec-revisit", "DEC-001", "競合が想定より多いと分かった")
        self.e.stop("implementer", "差し戻し")
        self.assertIn("（再確認）", self.e.next())
        self.assertTrue(denied(self.e.spawn("reviewer")))
        self.e.human_says("DEC-001 = B でお願いします")
        self.e.aq("dec-select", "DEC-001", "B")
        d = self.e.state()["design_decisions"][0]
        self.assertEqual((d["selected"], d["history"][-1]["selected"], d["history"][-1]["status"]),
                         ("B", "A", "superseded"))
        self.assertIn("設計方針（DEC）の選択が変わった後に、設計が更新されていない", self.e.next())
        self.assertIsNone(run_design(self.e))
        self.assertIn("実装が更新されていない", self.e.next())

    def test_supersede(self):
        self.e.spawn("design")
        self.e.start("design")
        self.e.as_role("design", *dec_args("DEC-003", "--supersedes", "DEC-001"))
        ds = {d["id"]: d for d in self.e.state()["design_decisions"]}
        self.assertEqual((ds["DEC-001"]["status"], ds["DEC-001"]["superseded_by"]), ("superseded", "DEC-003"))
        self.assertIn("置き換えた論点", self.e.aq("decisions"))

    def test_selected_decision_cannot_be_edited_silently(self):
        self.e.spawn("design")
        self.e.start("design")
        out = self.e.as_role("design", *dec_args(), ok=False)
        self.assertIn("dec-revisit", out)


class ReviewFixesTest(unittest.TestCase):
    """独立レビューで見つかった抜け: 別の返答での選択・名前での回答・表記の揺れ・選び直しの無効化・差し戻しとの順序。"""

    def setUp(self):
        self.e = Env()
        requirements_with_dec(self.e)
        _out, self.t = present(self.e)

    def tearDown(self):
        self.e.cleanup()

    def test_choice_in_a_separate_reply(self):
        self.e.human_says("DEC-001 = B でお願いします")
        self.e.human_says(f"承認 {self.t}")
        self.e.aq("spec", "--approve", "承認")
        self.assertEqual(self.e.state()["design_decisions"][0]["selected"], "B")

    def test_answer_by_option_name_without_id_is_not_approval(self):
        self.e.human_answers({"更新の整合性": "B（行ロックで直列化）"})
        self.e.human_says(f"承認 {self.t}")
        out = self.e.aq("spec", "--approve", "承認", ok=False)
        self.assertIn("推奨以外の案の名前", out)

    def test_notation_variants(self):
        self.e.human_says(f"承認 {self.t}\nｄｅｃ－１ ＝ ｂ")
        self.e.aq("spec", "--approve", "承認")
        self.assertEqual(self.e.state()["design_decisions"][0]["selected"], "B")


class ReselectInvalidatesTest(unittest.TestCase):
    def test_gate_and_review_become_stale(self):
        from helpers import build_to_review  # noqa: F401
        e = Env()
        try:
            requirements_with_dec(e)
            _o, t = present(e)
            e.human_says(f"承認 {t}")
            e.aq("spec", "--approve", "承認")
            self.assertIsNone(run_design(e))
            e.spawn("implementer")
            e.start("implementer")
            implement(e, MEMO_V1)
            self.assertIsNone(e.stop("implementer", "実装"))
            e.aq("gate")
            gate_fp = e.state()["gate"]["decisions_fp"]
            e.spawn("design")
            e.start("design")
            e.as_role("design", "dec-revisit", "DEC-001", "前提が変わった")
            e.stop("design", "差し戻し")
            e.human_says("DEC-001 = B")
            e.aq("dec-select", "DEC-001", "B")
            self.assertNotEqual(gate_fp, DD.decisions_fp(e.state()))
            # 同じ設計書では登録できない（選んだ案の行がない）
            res = run_design(e, ids=())
            self.assertIn("DEC-001 = B", (res or {}).get("reason", ""))
            e.write_as("design", os.path.relpath(os.path.join(e.arts, "design.md"), e.project),
                       design_md(e, ("DEC-001",)))
            self.assertIsNone(e.stop("design", "選び直した案で設計を更新"))
            e.spawn("implementer")
            e.start("implementer")
            implement(e, MEMO_V1)
            self.assertIsNone(e.stop("implementer", "同じコードで登録し直す"))
            self.assertIn("設計方針（DEC）の選択", e.next())  # 古い gate は使えない
        finally:
            e.cleanup()


class EscalationOrderTest(unittest.TestCase):
    def test_pending_decision_blocks_design_even_with_open_escalation(self):
        e = Env()
        try:
            requirements_with_dec(e)
            _o, t = present(e)
            e.human_says(f"承認 {t}")
            e.aq("spec", "--approve", "承認")
            self.assertIsNone(run_design(e))
            st = e.state()
            st["escalations"].append({"id": "E1", "status": "open", "from": "implementer", "text": "x"})
            st["design_decisions"][0].update(status="proposed", revisit={"by": "implementer", "reason": "x", "seq": 1})
            e.save_state(st)
            self.assertTrue(denied(e.spawn("design")))
            self.assertIn("未決定の設計方針", e.next())
        finally:
            e.cleanup()


class SupersedeKeepsSpecTest(unittest.TestCase):
    def test_superseding_by_design_keeps_approved_spec(self):
        e = Env()
        try:
            requirements_with_dec(e)
            _o, t = present(e)
            e.human_says(f"承認 {t}")
            e.aq("spec", "--approve", "承認")
            e.spawn("design")
            e.start("design")
            e.as_role("design", *dec_args("DEC-002", "--supersedes", "DEC-001"))
            st = e.state()
            from aqdlc_core.state_store import spec_hash
            self.assertEqual(spec_hash(st, e.run_dir), st["spec"]["approved_hash"])
        finally:
            e.cleanup()


class ChoiceParseTest(unittest.TestCase):
    def test_forms(self):
        keys = ["A", "B", "C"]
        for text, want in [("DEC-001 = B", "B"), ("DEC-001：C", "C"), ("DEC-001 は案A で", "A"),
                           ("承認 SPEC-1\nDEC-001=Ｂ", "B"), ("関係ない返答", None), ("DEC-0011 = B", None)]:
            with self.subTest(text=text):
                self.assertEqual(DD.choice_in(text, "DEC-001", keys)[0], want)

    def test_ambiguous(self):
        keys = ["A", "B"]
        for text in ("DEC-001 = A、いや DEC-001 = B", "DEC-001 = B ではなく A", "DEC-001 は保留", "DEC-001 = C",
                     "確認したい選択: DEC-001 = A / B → B でお願いします", "DEC-001 は Bにしようと思ったけど、やっぱり推奨のAで",
                     "DEC-001 = B。いや、やっぱり推奨で", "DEC-001 = B（推奨で）"):
            with self.subTest(text=text):
                got, why = DD.choice_in(text, "DEC-001", keys)
                self.assertIsNone(got)
                self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
