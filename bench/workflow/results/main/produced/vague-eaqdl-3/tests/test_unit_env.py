"""V27 V28: 環境・成果物の機械検査。"""
import ast
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL = {"notes", "tests"}


def _py_files() -> list[pathlib.Path]:
    return sorted(p for d in ("notes", "tests") for p in (ROOT / d).glob("*.py"))


def _imported_modules(path: pathlib.Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return mods


class EnvTest(unittest.TestCase):
    def test_env_importは標準ライブラリと自パッケージだけ(self) -> None:
        files = _py_files()
        self.assertTrue(any(p.name == "service.py" for p in files))
        for path in files:
            for mod in _imported_modules(path):
                self.assertTrue(mod in sys.stdlib_module_names or mod in LOCAL or mod == "__future__",
                                msg=f"{path.name} が標準外のモジュール {mod} を import している")

    def test_env_必要なファイルがある(self) -> None:
        for name in ("service.py", "ratelimit.py", "share.py", "db.py", "errors.py"):
            self.assertTrue((ROOT / "notes" / name).is_file(), name)

    def test_env_db_errorsに差分がない(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git が使えないため差分検査を実施できない")
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                                capture_output=True, text=True, check=False)
        if inside.returncode != 0:
            self.skipTest("git リポジトリではないため差分検査を実施できない")
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "notes/db.py", "notes/errors.py"],
                                 cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        diff = subprocess.run(["git", "diff", "HEAD", "--", "notes/db.py", "notes/errors.py"], cwd=ROOT,
                              capture_output=True, text=True, check=False)
        self.assertEqual(diff.returncode, 0)
        self.assertEqual(diff.stdout, "")

    def test_env_テストのDB接続は一時領域かメモリだけ(self) -> None:
        # 文字列リテラルで固定パスを渡す connect は不可（:memory: か一時ディレクトリ由来の変数のみ）
        for path in sorted((ROOT / "tests").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in ("connect", "open_db") or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    self.assertEqual(first.value, ":memory:", msg=f"{path.name}:{node.lineno}")
                elif isinstance(first, ast.JoinedStr):
                    self.fail(f"{path.name}:{node.lineno} 文字列整形でパスを作っている")
        leftovers = [p for pat in ("*.db", "*.sqlite", "*.sqlite3") for p in ROOT.rglob(pat)
                     if not any(part.startswith(".") for part in p.relative_to(ROOT).parts)]
        self.assertEqual(leftovers, [])
