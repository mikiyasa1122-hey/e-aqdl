"""state の保存と判定の分離（ARCH-02）の確認。

- 判定（state_policy）は、ファイル・git に触れずに動く（事実は Ctx から受け取る）
- 本物の Ctx と、同じ事実を持つ偽の Ctx で、判定の結果が同じ
- 事実を変えると、判定の主要な分岐（gate の鮮度・実装ファイルの欠落・CI・実行中の担当）が変わる
- 依存の向き: state_policy は state_store・state_view・state を import しない
"""
import ast
import builtins
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import REVIEW_PASS, ROOT, Env, build_to_review  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import state_policy as SP  # noqa: E402
from aqdlc_core import util as U  # noqa: E402
from aqdlc_core.state import Ctx, load_state  # noqa: E402

CORE = os.path.join(ROOT, "plugins", "aq-dlc", "scripts", "aqdlc_core")


class FakeCtx:
    """判定に必要な事実だけを持つ Ctx（ファイル・git を読まない）。"""

    def __init__(self, real, **over):
        for k in ("project", "state", "cfg", "config_fp", "rd", "spec_hash", "design_hash", "graph_fp", "checks",
                  "checks_fp", "fresh"):
            setattr(self, k, getattr(real, k))
        self.snap, self.code_fp = real.snap, real.code_fp
        self._facts = {"holdout_fp": real.holdout_fp(), "reviewer_role_fp": real.reviewer_role_fp(),
                       "ci_head": real.ci_head(), "missing": set()}
        self.cf = lambda rec: []
        self.__dict__.update(over)

    def file_exists(self, rel):
        return rel not in self._facts["missing"]

    def holdout_fp(self):
        return self._facts["holdout_fp"]

    def reviewer_role_fp(self):
        return self._facts["reviewer_role_fp"]

    def ci_head(self):
        return self._facts["ci_head"]

    def cf_stale_reasons(self, rec):
        return self.cf(rec)


def no_io():
    """判定の間に、ファイル・パス・外部コマンドに触れたら失敗させる。"""
    def boom(*a, **k):
        raise AssertionError(f"判定の中で I/O が起きた: {a[:1]}")
    return [mock.patch.object(builtins, "open", boom), mock.patch.object(os.path, "exists", boom),
            mock.patch.object(os.path, "isdir", boom), mock.patch.object(os.path, "isfile", boom),
            mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom),
            mock.patch.object(U, "sh_run", boom), mock.patch.object(U, "read_text", boom),
            mock.patch.object(U, "load_json", boom)]


def judge(ctx, stage="pr"):
    patches = no_io()
    for p in patches:
        p.start()
    try:
        return [(x["code"], x["route"]) for x in SP.problems(ctx, stage)]
    finally:
        for p in patches:
            p.stop()


class PolicyWithoutIOTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = Env()
        build_to_review(cls.e, with_question=False)
        old = dict(os.environ)
        os.environ.update(cls.e.env)
        try:
            state, _ = load_state(cls.e.project)
            cls.real = Ctx(cls.e.project, state)
            cls.expected = [(x["code"], x["route"]) for x in SP.problems(cls.real, "pr")]
        finally:
            os.environ.clear()
            os.environ.update(old)

    @classmethod
    def tearDownClass(cls):
        cls.e.cleanup()

    def fake(self, **over):
        return FakeCtx(self.real, **over)

    def test_same_result_as_real_ctx_without_io(self):
        self.assertTrue(self.expected)  # レビュー前なので、少なくともレビューが足りない
        self.assertEqual(judge(self.fake()), self.expected)

    def test_code_changed_without_implementer(self):
        """gate の後にコードの指紋が変わった（実装担当の記録がない）→ 担当外の変更として扱う。"""
        codes = [c for c, _ in judge(self.fake(code_fp="0" * 40))]
        self.assertIn("code_unowned", codes)

    def test_gate_becomes_stale_when_checks_change(self):
        self.assertNotIn("gate_stale", [c for c, _ in self.expected])
        codes = [c for c, _ in judge(self.fake(checks_fp="changed"))]
        self.assertIn("gate_stale", codes)

    def test_missing_impl_file(self):
        f = self.fake()
        f._facts["missing"] = {"src/memo.py"}
        self.assertIn(("impl_untraced", "implementer"), judge(f))

    def test_running_agents_uses_given_time(self):
        st = {"agents": {"running": {"a": {"role": "reviewer", "epoch": 1000}}}}
        self.assertEqual(list(SP.running_agents(st, now=1000 + 60)), ["a"])
        self.assertEqual(SP.running_agents(st, now=1000 + 4 * 3600), {})

    def test_validate_state_is_pure_and_detects_mismatch(self):
        import copy
        s = copy.deepcopy(self.real.state)
        name = next(iter(s["gate"]["results"]["checks"]))
        s["gate"]["results"]["checks"][name]["pass"] = False  # status は passed のまま（食い違い）
        patches = no_io()
        for p in patches:
            p.start()
        try:
            errs = SP.validate_state(s)
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(any("食い違" in e for e in errs), errs)


class CiFactTest(unittest.TestCase):
    """レビュー合格・Memory 整理の後に来る CI の判定。CI の対象のコミットは Ctx の事実。"""

    @classmethod
    def setUpClass(cls):
        e = cls.e = Env()
        build_to_review(e, with_question=False)
        e.spawn("reviewer")
        e.start("reviewer")
        assert e.stop("reviewer", REVIEW_PASS) is None
        e.aq("memory-done", "--end", "none", "--skipped", "テスト")
        old = dict(os.environ)
        os.environ.update(e.env)
        try:
            state, _ = load_state(e.project)
            cls.real = Ctx(e.project, state)
        finally:
            os.environ.clear()
            os.environ.update(old)

    @classmethod
    def tearDownClass(cls):
        cls.e.cleanup()

    def test_ci(self):
        f = FakeCtx(self.real)
        self.assertIsNone(f._facts["ci_head"])  # CI の設定・リモートがない
        self.assertNotIn("ci", [c for c, _ in judge(f)])
        f._facts["ci_head"] = "a" * 40  # CI があり、HEAD に対する結果がまだない
        self.assertIn(("ci", "ci"), judge(f))
        f.state = dict(f.state, ci={"sha": "a" * 40, "result": "failure"})
        self.assertIn("ci", [c for c, _ in judge(f)])  # 失敗 → 実装担当へ
        f.state = dict(f.state, ci={"sha": "a" * 40, "result": "success"})
        self.assertNotIn("ci", [c for c, _ in judge(f)])


class DirectionTest(unittest.TestCase):
    def imports(self, name):
        tree = ast.parse(open(os.path.join(CORE, name + ".py"), encoding="utf-8").read())
        return {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1}

    def test_policy_does_not_reach_store_or_view(self):
        for m in ("state_policy", "state_model"):
            self.assertFalse(self.imports(m) & {"state", "state_store", "state_view"}, m)
        self.assertFalse(self.imports("state_view") & {"state", "state_store"})

    def test_state_keeps_public_names(self):
        """既存の import 先（from .state import ...）を壊さない。"""
        from aqdlc_core import state as S
        for name in ("Ctx", "StateError", "load_state", "save_state", "require_state", "run_dir", "new_run", "log",
                     "bump", "human_event", "iter_verifications", "problems", "next_action", "gate_status",
                     "validate_state", "spec_sheet", "trace_rows", "describe_state", "pending_by_sheet",
                     "recommended_option", "find", "ensure_new_id", "req_by_id", "ROLE_LABEL", "ROLES", "migrate"):
            self.assertTrue(hasattr(S, name), name)


if __name__ == "__main__":
    unittest.main()
