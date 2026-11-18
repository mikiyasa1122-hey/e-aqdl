"""改善A: Quality Gate の鮮度。

- gate は、検査の対象（内容の指紋。未コミット・未追跡を含む）と、HEAD・作業ツリーの状態を記録する
- push・PR の作成／更新の前に、最新のソースと合格した gate の対象を照合する。違えば拒否する（古い合格を流用しない）
  - gate の後にコード・チェック・設定・検証条件が変わった（gate_stale）
  - 未コミットの変更がある（push される内容が、gate が検査した内容と一致しない）
  - gate がない・不合格・実行中に変更があった
- 変更がなければ、同じソースの状態に対する合格を再利用できる（コミットしても内容が同じなら有効）
- 完了報告に、対象の識別子・各検査の結果・証跡の場所・鮮度を出す
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ENGINE, MEMO_V2, Env, build_to_review, denied  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(ENGINE)))
from aqdlc_core import gate as G  # noqa: E402
from aqdlc_core import gitops  # noqa: E402


def bash(e, cmd, role=None):
    return e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": "t-pub",
                               **e.agent_fields(role)})


def reason(res):
    return ((res or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.e = Env(git=True)
        self.e.git("checkout", "-q", "-b", "feat")  # 作業ブランチ（push する参照）
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def commit_all(self, msg="作業"):
        self.e.git("add", "-A")
        self.e.git("commit", "-q", "-m", msg)

    def test_gate_records_source_identity(self):
        g = self.e.state()["gate"]
        src = g["source"]
        self.assertEqual(src["code_fp"], g["code_fp"])
        self.assertGreater(src["files"], 0)
        self.assertIsNone(src["git"]["head"])  # まだコミットがない
        self.assertGreater(src["git"]["untracked"], 0)
        self.commit_all()
        self.e.aq("gate")
        g = self.e.state()["gate"]
        head = self.e.git("rev-parse", "HEAD").strip()
        self.assertEqual(g["source"]["git"]["head"], head)
        self.assertEqual(g["source"]["git"]["branch"], "feat")
        self.assertEqual(g["source"]["git"]["dirty_source"], [])
        self.assertEqual(self.e.state()["gate_history"][-1]["head"], head)

    def test_uncommitted_changes_block_push_until_committed(self):
        res = bash(self.e, "git push -u origin feat")
        self.assertTrue(denied(res))
        self.assertIn("コミットしていない変更", reason(res))
        self.commit_all()  # 内容は gate のときと同じ → gate はそのまま有効
        self.assertFalse(denied(bash(self.e, "git push -u origin feat")))

    def test_change_after_gate_blocks_push_and_pr(self):
        self.commit_all()
        self.assertFalse(denied(bash(self.e, "git push -u origin feat")))
        with open(os.path.join(self.e.project, "src", "memo.py"), "w", encoding="utf-8") as f:
            f.write(MEMO_V2)
        self.commit_all("gate の後の変更")
        res = bash(self.e, "git push -u origin feat")
        self.assertTrue(denied(res))
        self.assertIn("gate 後に変わったものがある", reason(res))
        # PR は complete と同じ条件で照合する（ここでは、先に「実装担当以外の変更」が問題として出る）
        for cmd in ("gh pr create --title t --body b", "gh pr edit 1 --body b"):
            with self.subTest(cmd=cmd):
                res = bash(self.e, cmd)
                self.assertTrue(denied(res))
                self.assertIn("PR の作成・更新の前の照合", reason(res))

    def test_new_untracked_source_file_blocks(self):
        self.commit_all()
        with open(os.path.join(self.e.project, "src", "extra.py"), "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        res = bash(self.e, "git push")
        self.assertTrue(denied(res))
        self.assertIn("src/extra.py", reason(res))

    def test_ignored_and_workflow_files_do_not_block(self):
        self.commit_all()
        with open(os.path.join(self.e.project, ".gitignore"), "a", encoding="utf-8") as f:
            f.write("build/\n")
        self.commit_all("ignore")
        self.e.aq("gate")
        os.makedirs(os.path.join(self.e.project, "build"), exist_ok=True)
        with open(os.path.join(self.e.project, "build", "out.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.e.aq("status")  # .aq-dlc/ の記録が変わっても、照合の対象ではない
        self.assertFalse(denied(bash(self.e, "git push -u origin feat")))

    def test_failed_or_missing_gate_blocks_push(self):
        self.commit_all()
        st = self.e.state()
        st.pop("gate")
        self.e.save_state(st)
        res = bash(self.e, "git push")
        self.assertTrue(denied(res))
        self.assertIn("Quality Gate が未実行", reason(res))

    def test_pr_requires_full_completion_conditions(self):
        """PR の作成は、complete と同じ条件（独立レビューの合格など）を求める。push は gate の鮮度だけ。"""
        self.commit_all()
        self.assertFalse(denied(bash(self.e, "git push -u origin feat")))
        res = bash(self.e, "gh pr create --title t --body b")
        self.assertTrue(denied(res))
        self.assertIn("レビュー", reason(res))

    def test_unrelated_commands_are_not_checked(self):
        for cmd in ("git status", "git log --oneline", "gh pr view 1", "git push origin --delete old-branch"):
            with self.subTest(cmd=cmd):
                self.assertNotIn("照合", reason(bash(self.e, cmd)))

    def test_switch_off(self):
        from helpers import edit_json
        edit_json(os.path.join(self.e.project, ".aq-dlc", "config.json"),
                  lambda c: c.setdefault("automation", {}).update({"block_stale_publish": False}))
        self.e.aq("config-accept", ok=None)
        self.assertNotIn("照合", reason(bash(self.e, "git push -u origin feat")))

    def test_report_shows_identity_and_freshness(self):
        self.commit_all()
        out = self.e.aq("report")
        self.assertIn("## 検査の対象と鮮度", out)
        self.assertIn("内容の指紋 " + self.e.state()["gate"]["code_fp"][:12], out)
        self.assertIn("チェック: unit 合格", out)
        self.assertIn("鮮度: 有効", out)
        with open(os.path.join(self.e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
            f.write("\n# 変更\n")
        out = self.e.aq("report")
        self.assertIn("期限切れ（stale）", out)


class ReviewFixesTest(unittest.TestCase):
    """独立レビューで見つかった抜け（push 先の参照・complete の後・サブフォルダ・削除・書き方の揺れ）。"""

    def setUp(self):
        self.e = Env(git=True)
        build_to_review(self.e, with_question=False)
        self.e.git("add", "-A")
        self.e.git("commit", "-q", "-m", "gate の対象")

    def tearDown(self):
        self.e.cleanup()

    def test_pushing_another_ref_is_checked(self):
        e = self.e
        e.git("switch", "-q", "-c", "bad")
        with open(os.path.join(e.project, "src", "memo.py"), "w", encoding="utf-8") as f:
            f.write(MEMO_V2)
        e.git("commit", "-qam", "gate を通っていない")
        e.git("switch", "-q", "main")
        for cmd in ("git push origin bad:feat", "git push origin bad"):
            with self.subTest(cmd=cmd):
                res = bash(e, cmd)
                self.assertTrue(denied(res))
                self.assertIn("gate の対象（今の HEAD）と違う", reason(res))
        res = bash(e, "git push --all origin")
        self.assertTrue(denied(res))
        self.assertIn("push する参照を特定できない", reason(res))
        self.assertFalse(denied(bash(e, "git push -u origin main")))

    def test_checked_after_complete(self):
        st = self.e.state()
        st["status"] = "complete"
        self.e.save_state(st)
        with open(os.path.join(self.e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
            f.write("\n# complete の後の変更\n")
        for cmd in ("git push", "gh pr create --fill"):
            with self.subTest(cmd=cmd):
                self.assertTrue(denied(bash(self.e, cmd)))

    def test_deleted_tracked_file_blocks(self):
        os.remove(os.path.join(self.e.project, "tests", "test_memo.py"))
        res = bash(self.e, "git push")
        self.assertTrue(denied(res))

    def test_command_variants(self):
        with open(os.path.join(self.e.project, "src", "extra.py"), "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        for cmd in ("timeout 60 git push", "bash -c 'git push'", "(git push)", "env -u X git push", "sudo -E git push",
                    "eval git push", "git -c alias.p=push p", "gh pr -R o/r create", "gh api -X POST repos/o/r/pulls",
                    "true & git push"):
            with self.subTest(cmd=cmd):
                self.assertTrue(denied(bash(self.e, cmd)), cmd)


class SubfolderProjectTest(unittest.TestCase):
    def test_project_inside_a_larger_repository(self):
        e = Env()
        try:
            e.git("init", "-q", "-b", "main", cwd=e.tmp)  # リポジトリのルートはプロジェクトの親
            build_to_review(e, with_question=False)
            st = G.git_state(e.project, {"gate": {"ignore_dirs": []}, "quality": {}})
            self.assertIn("src/memo.py", st["dirty_source"])
            self.assertTrue(denied(bash(e, "git push")))
            e.git("add", "-A", cwd=e.tmp)
            e.git("commit", "-q", "-m", "all", cwd=e.tmp)
            with open(os.path.join(e.tmp, "outside.txt"), "w", encoding="utf-8") as f:
                f.write("プロジェクトの外の変更")
            self.assertEqual(G.git_state(e.project, {"gate": {"ignore_dirs": []}, "quality": {}})["dirty_source"], [])
        finally:
            e.cleanup()


class PublishProblemsUnitTest(unittest.TestCase):
    def test_git_error_blocks(self):
        from aqdlc_core import state_policy as SP
        e = Env(git=True)
        try:
            build_to_review(e, with_question=False)
            from aqdlc_core.state_store import Ctx
            from aqdlc_core.state_store import load_state
            st, _ = load_state(e.project)
            probs = SP.publish_problems(Ctx(e.project, st), {"error": "git status に失敗した"}, "push")
            self.assertIn("publish_git", [p["code"] for p in probs])
        finally:
            e.cleanup()


class GitStateTest(unittest.TestCase):
    def test_counts_and_renames(self):
        e = Env(git=True)
        try:
            p = e.project
            for n in ("a.py", "b.py", "c.py"):
                with open(os.path.join(p, n), "w", encoding="utf-8") as f:
                    f.write(n)
            e.git("add", "-A")
            e.git("commit", "-q", "-m", "init")
            cfg = {"gate": {"ignore_dirs": []}, "quality": {}}
            st = G.git_state(p, cfg)
            self.assertEqual((st["staged"], st["unstaged"], st["untracked"], st["dirty_source"]), (0, 0, 0, []))
            e.git("mv", "a.py", "a2.py")
            with open(os.path.join(p, "b.py"), "w", encoding="utf-8") as f:
                f.write("changed")
            os.remove(os.path.join(p, "c.py"))
            with open(os.path.join(p, "new.py"), "w", encoding="utf-8") as f:
                f.write("n")
            st = G.git_state(p, cfg)
            self.assertEqual(st["staged"], 1)
            self.assertEqual(st["unstaged"], 2)
            self.assertEqual(st["untracked"], 1)
            self.assertEqual(st["dirty_source"], ["a2.py", "b.py", "c.py", "new.py"])
        finally:
            e.cleanup()

    def test_not_a_repository(self):
        e = Env(git=False)
        try:
            st = G.git_state(e.project, {"gate": {"ignore_dirs": []}, "quality": {}})
            self.assertIsNone(st["head"])
        finally:
            e.cleanup()


class PublishKindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {"git push -u origin feat": "push", "git -C sub push": "push", "gh pr create --fill": "pr",
                 "git push && gh pr create": "pr", "gh pr ready 3": "pr", "gh pr view 3": None,
                 "git push origin --delete x": None, "git commit -m 'push'": None}
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(gitops.publish_kind(cmd), want)


if __name__ == "__main__":
    unittest.main()
