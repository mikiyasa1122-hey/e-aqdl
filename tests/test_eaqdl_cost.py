"""低コスト化 P0（使用量の計測）・P1（引き継ぎの重複削減・gate の fail-fast）のテスト。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Env, build_to_review  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import agents as A  # noqa: E402
from aqdlc_core import gate as G  # noqa: E402
from aqdlc_core import roles as RL  # noqa: E402


def write_transcript(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def usage_row(mid, ts, inp=10, out=5, cr=100, cc=20, model="claude-test"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": model, "role": "assistant",
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}}}


class TranscriptUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.jsonl")

    def test_missing_is_none_not_zero(self):
        self.assertIsNone(A.transcript_usage(None))
        self.assertIsNone(A.transcript_usage(os.path.join(self.tmp, "none.jsonl")))
        write_transcript(self.path, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
        self.assertIsNone(A.transcript_usage(self.path))  # usage が1つもない

    def test_dedup_window_calls_models(self):
        write_transcript(self.path, [
            usage_row("m1", "2026-11-01T00:00:00Z"),
            usage_row("m1", "2026-11-01T00:00:00Z"),  # 同じ API 呼び出しが2行に分かれた記録 → 1回
            usage_row("m2", "2026-11-01T01:00:00Z", model="claude-other"),
            usage_row("m3", "2026-11-01T03:00:00Z"),
        ])
        t = A.transcript_usage(self.path)
        self.assertEqual(t["api_calls"], 3)
        self.assertEqual(t["input"], 30)
        self.assertEqual(t["cache_read"], 300)
        self.assertEqual(t["models"], ["claude-other", "claude-test"])
        w = A.transcript_usage(self.path, since="2026-11-01T00:30:00+00:00", until="2026-11-01T02:00:00+00:00")
        self.assertEqual(w["api_calls"], 1)  # 期間外の呼び出し（別の run の分）を混ぜない
        self.assertEqual(w["models"], ["claude-other"])
        self.assertIsNone(A.transcript_usage(self.path, since="2026-11-02T00:00:00+00:00"))


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_metrics_schema_and_missing_usage(self):
        e = self.e
        build_to_review(e, with_question=False)
        m = json.loads(e.aq("metrics", "--json"))
        self.assertEqual(m["schema"], 2)
        self.assertIn("engine_version", m)
        stages = {x["stage"]: x for x in m["stages"]}
        for k in ("requirements", "design", "implementation", "review", "gate", "counterfactual", "memory"):
            self.assertIn(k, stages)
        # テストのサブエージェントには会話記録がない → 使用量は 0 ではなく None
        self.assertIsNone(stages["implementation"]["tokens"])
        self.assertFalse(stages["implementation"]["usage_complete"])
        self.assertFalse(m["tokens_complete"])
        self.assertIsNone(m["quality"]["external_acceptance_pass_rate"])  # エンジンは捏造しない
        self.assertIsNone(m["quality"]["critical_defects"])
        self.assertIsInstance(m["quality"]["unverified"], int)
        self.assertIn("rework", m)
        # 再集計しても同じ（二重計上しない）
        m2 = json.loads(e.aq("metrics", "--json"))
        self.assertEqual(m["roles"], m2["roles"])
        self.assertEqual(m["stages"], m2["stages"])
        out = e.aq("metrics")
        self.assertIn("取得できず", out)
        self.assertIn("料金ではありません", out)

    def test_orchestrator_usage_counts_only_this_run(self):
        e = self.e
        build_to_review(e, with_question=False)
        s = e.state()
        tp = os.path.join(e.tmp, "orch.jsonl")
        write_transcript(tp, [usage_row("old", "2000-01-01T00:00:00Z"),  # 別の（以前の）run の分
                              usage_row("now", "2099-01-01T00:00:00Z")])
        s["session_transcript"] = tp
        s["created"] = "2026-01-01T00:00:00+00:00"
        e.save_state(s)
        m = json.loads(e.aq("metrics", "--json"))
        self.assertEqual(m["orchestrator_tokens"]["api_calls"], 1)
        self.assertEqual(m["orchestrator_tokens_session"]["api_calls"], 2)

    def test_metrics_has_no_conversation_text(self):
        e = self.e
        build_to_review(e, with_question=False)
        e.human_says("秘密の合言葉 sk_test_SHOULD_NOT_APPEAR")
        out = e.aq("metrics", "--json")
        self.assertNotIn("SHOULD_NOT_APPEAR", out)

    def test_runs_handles_old_metrics_schema(self):
        e = self.e
        build_to_review(e, with_question=False)
        s = e.state()
        s["metrics"] = {"run_sec": 1, "roles": {}, "gate": {"failed": 0}, "fix_iterations": 0,
                        "reviews": {"needs_fix": 0}, "counterfactual": {}, "human": {"asks": 0},
                        "conditions": {"memory_scope": "project", "counterfactual_mode": None,
                                       "roles_enabled": True, "engine": "E-AQDL"}}  # 旧版で保存された形
        e.save_state(s)
        rows = json.loads(e.aq("metrics", "--runs", "--json"))
        self.assertEqual(len(rows), 1)
        self.assertIn("tokens_input_total", rows[0])
        self.assertIsNone(rows[0]["tokens_input_total"])  # 取得できない → None
        self.assertIn("取得できず", e.aq("metrics", "--runs"))


class CompactHandoffTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.old_home = os.environ.get("AQDLC_HOME")
        os.environ["AQDLC_HOME"] = self.e.home

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("AQDLC_HOME", None)
        else:
            os.environ["AQDLC_HOME"] = self.old_home
        self.e.cleanup()

    def briefs(self, compact):
        from aqdlc_core.state import Ctx, load_state
        s, _ = load_state(self.e.project)
        import copy
        ctx = Ctx(self.e.project, s)
        ctx.cfg = copy.deepcopy(ctx.cfg)  # 既定値の辞書を書き換えない
        ctx.cfg.setdefault("cost_optimization", {})["compact_handoff"] = compact
        return {r: A.brief(r, ctx, {} if r == "reviewer" else None, A.role_selection(r, ctx))
                for r in ("requirements", "design", "implementer", "reviewer")}

    def test_default_is_off_and_full(self):
        build_to_review(self.e, with_question=False)
        from aqdlc_core.project import config
        self.assertFalse(config(self.e.project)["cost_optimization"]["compact_handoff"])
        self.assertFalse(config(self.e.project)["cost_optimization"]["gate_fail_fast"])
        full = self.briefs(False)
        self.assertIn("禁止: テストを通すために検証条件を弱める", full["implementer"])
        self.assertIn("state.json と config.json は直接編集しない", full["implementer"])

    def test_compact_keeps_run_specific_information(self):
        build_to_review(self.e, with_question=False)
        full, comp = self.briefs(False), self.briefs(True)
        s = self.e.state()
        for role in full:
            self.assertLessEqual(len(comp[role]), len(full[role]), role)
        imp = comp["implementer"]
        self.assertNotIn("禁止: テストを通すために", imp)  # 一般論はエージェント定義にある
        self.assertIn(s["spec"]["registered_hash"][:8], imp)  # 読む版の指紋は残す
        self.assertIn("R1", imp)  # 要求
        self.assertIn("Quality Gate のチェック", imp)
        self.assertIn("エージェント定義", imp)
        # エージェント定義には、省いた共通の決まりがある
        for name in ("requirements", "design", "implementer", "reviewer"):
            with open(os.path.join(ROOT, "plugins", "aq-dlc", "agents", f"{name}.md"), encoding="utf-8") as f:
                md = f.read()
            self.assertIn("state.json", md, name)
            self.assertIn("人間には直接質問できない", md, name)
            self.assertIn("日本語", md, name)

    def test_compact_domain_profile_keeps_rules(self):
        sel = {"enabled": True, "stage": [], "fallback": False,
               "domain": [{"id": "security", "title": "Security", "version": 1, "reasons": ["k"], "related": []}],
               "_bodies": {"security": "# Security\n## 責務\n- 責務A\n## 判断基準\n- 基準B\n## 禁止事項\n- 禁止C\n"
                                       "## 成果物\n- 成果物D\n## 検証条件\n- 検証E\n## 引き継ぎ条件\n- 引継F\n"}}
        full = "\n".join(RL.brief_section(sel))
        comp = "\n".join(RL.brief_section(sel, compact=True))
        for x in ("基準B", "禁止C", "検証E"):
            self.assertIn(x, comp)
        for x in ("責務A", "成果物D", "引継F"):
            self.assertIn(x, full)
            self.assertNotIn(x, comp)

    def test_default_cost_section_does_not_change_config_fingerprint(self):
        cfg = {"checks": [], "gate": {}}
        from aqdlc_core.project import DEFAULTS
        with_default = dict(cfg, cost_optimization=DEFAULTS["cost_optimization"])
        self.assertEqual(G.config_fp(cfg), G.config_fp(with_default))
        changed = dict(cfg, cost_optimization={"compact_handoff": True, "gate_fail_fast": False})
        self.assertNotEqual(G.config_fp(cfg), G.config_fp(changed))


class GateFailFastTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.marker = os.path.join(self.tmp, "ran-integration")

    def run_gate(self, fail_fast):
        py = f'"{sys.executable}"'
        touch = f'{py} -c "open(r\'{self.marker}\', \'w\').close()"'
        cfg = {"checks": [{"name": "unit", "command": f'{py} -c "import sys; sys.exit(1)"', "kind": "unit"},
                          {"name": "integ", "command": touch, "kind": "integration"}],
               "gate": {"default_timeout_sec": 60, "env": {}}, "cost_optimization": {"gate_fail_fast": fail_fast}}
        state = {"requirements": [{"id": "R1", "acceptance": [{"id": "AC1", "verifications": [
            {"id": "V1", "level": "integration", "command": touch}]}], "risks": []}]}

        def iv(st):
            for r in st["requirements"]:
                for p in r["acceptance"]:
                    for v in p["verifications"]:
                        yield r, p, v
        # gate は日本語の進捗を表示する。Windows の既定のコンソール（cp1252）で落ちないよう、表示は捨てる
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return G.run_gate(self.tmp, state, cfg, iv, os.path.join(self.tmp, "logs"), "s1")

    def test_fail_fast_skips_heavy_checks_without_passing_them(self):
        res, ok = self.run_gate(True)
        self.assertFalse(ok)  # gate は不合格のまま
        self.assertFalse(res["checks"]["integ"]["pass"])  # 実行しなかったものを合格にしない
        self.assertTrue(res["checks"]["integ"]["fail_fast"])
        self.assertFalse(res["verifications"]["V1"]["pass"])
        self.assertTrue(res["verifications"]["V1"]["fail_fast"])
        self.assertFalse(os.path.exists(self.marker))  # 実際に実行していない

    def test_default_runs_everything(self):
        res, ok = self.run_gate(False)
        self.assertFalse(ok)
        self.assertTrue(res["checks"]["integ"]["pass"])
        self.assertTrue(os.path.exists(self.marker))


if __name__ == "__main__":
    unittest.main()
