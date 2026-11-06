"""CLI・hook の入口と、業務の処理を分けたこと（ARCH-01）の確認。

- hooks.json が呼ぶ hook の名前が、すべて入口（hook_handlers.HOOKS）にある
- CLI の dispatch（cli.COMMANDS）は、移した先の関数をそのまま呼ぶ
- 移した処理は cli を import せずに使える（dispatch と業務の処理を別々にテストできる）
- hook の入力（JSON）と出力（JSON）・終了コードの形は従来どおり
"""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ENGINE, ROOT, Env, denied  # noqa: E402

SCRIPTS = os.path.join(ROOT, "plugins", "aq-dlc", "scripts")
sys.path.insert(0, SCRIPTS)


def hook_names_in_manifest():
    with open(os.path.join(ROOT, "plugins", "aq-dlc", "hooks", "hooks.json"), encoding="utf-8") as f:
        data = json.load(f)
    names = set()
    for entries in data["hooks"].values():
        for e in entries:
            for h in e["hooks"]:
                names.add(h["command"].split(" hook ")[1].strip().strip('"'))
    return names


class DispatchTest(unittest.TestCase):
    def test_every_manifest_hook_is_dispatched(self):
        from aqdlc_core import hook_handlers as HH
        self.assertEqual(hook_names_in_manifest() - set(HH.HOOKS), set())

    def test_cli_dispatches_to_moved_functions(self):
        from aqdlc_core import cli
        from aqdlc_core.commands import gate, metrics, spec
        self.assertIs(cli.COMMANDS["gate"], gate.cmd_gate)
        self.assertIs(cli.COMMANDS["spec"], spec.cmd_spec)
        self.assertIs(cli.COMMANDS["metrics"], metrics.cmd_metrics)

    def test_moved_modules_do_not_import_cli(self):
        """移した処理は cli を読み込まずに使える（逆向きの依存がない）。"""
        code = ("import sys; import aqdlc_core.hook_handlers, aqdlc_core.commands.gate, aqdlc_core.commands.spec, "
                "aqdlc_core.commands.metrics; print('aqdlc_core.cli' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=SCRIPTS, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")

    def test_unknown_command_and_hook(self):
        r = subprocess.run([sys.executable, ENGINE, "no-such-command"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)  # 従来どおり: 使い方を出して 2
        self.assertIn("E-AQDL エンジン", r.stdout)
        r = subprocess.run([sys.executable, ENGINE, "hook", "no-such-hook"], input="{}", capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0)  # 知らない hook は何もしない（Claude Code を止めない）


class MovedPathsTest(unittest.TestCase):
    def test_metrics_reports_engine_version(self):
        """移したモジュールからも、プラグインの版を読める（ファイルの場所に依存する処理の回帰）。"""
        with open(os.path.join(ROOT, "plugins", "aq-dlc", ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            version = json.load(f)["version"]
        e = Env()
        try:
            e.aq("init", "x")
            e.aq("new", "x")
            self.assertEqual(json.loads(e.aq("metrics", "--json"))["engine_version"], version)
        finally:
            e.cleanup()


class HookLogicWithoutCliTest(unittest.TestCase):
    """hook の処理を、CLI の dispatch を通さずに直接呼んで確かめる。"""

    def setUp(self):
        self.e = Env()
        self.e.aq("init", "x")
        self.e.aq("new", "x")

    def tearDown(self):
        self.e.cleanup()

    def call(self, fn, payload):
        old = dict(os.environ)
        os.environ.update(self.e.env)
        try:
            with redirect_stdout(io.StringIO()):
                return fn(payload)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_pre_write_denies_orchestrator_code_edit(self):
        from aqdlc_core.hook_handlers.pre_tool import hook_pre_write
        res = self.call(hook_pre_write, {"cwd": self.e.project, "tool_name": "Write",
                                         "tool_input": {"file_path": os.path.join(self.e.project, "src", "a.py"),
                                                        "content": "x"}})
        self.assertTrue(denied(res), res)  # 統括はコードを書けない（従来の判断のまま）

    def test_same_decision_through_cli_entry(self):
        payload = {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.e.project, "src", "a.py"),
                                                        "content": "x"}}
        self.assertTrue(denied(self.e.hook("pre-write", payload)))


if __name__ == "__main__":
    unittest.main()
