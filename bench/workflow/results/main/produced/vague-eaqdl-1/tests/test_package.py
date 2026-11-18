"""成果物の条件（V24）: 標準ライブラリのみ・db.py と errors.py が無変更・テストモジュールの存在。"""
import ast
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = {"notes", "tests"}


def _imported_modules(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class PackageTest(unittest.TestCase):
    def test_V24_標準ライブラリ以外をimportしない(self):
        checked = 0
        for folder in ("notes", "tests"):
            for name in sorted(os.listdir(os.path.join(ROOT, folder))):
                if name.endswith(".py"):
                    checked += 1
                    for mod in _imported_modules(os.path.join(ROOT, folder, name)):
                        with self.subTest(file=folder + "/" + name, module=mod):
                            self.assertTrue(mod in sys.stdlib_module_names or mod in LOCAL, mod)
        self.assertGreaterEqual(checked, 11)

    def test_V24_必要なファイルが揃っている(self):
        for rel in ("notes/service.py", "notes/ratelimit.py", "notes/share.py", "tests/__init__.py",
                    "tests/test_service_unit.py", "tests/test_service_integration.py", "tests/test_mutation.py",
                    "tests/test_ratelimit.py", "tests/test_share.py", "tests/test_package.py"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_V24_db_pyとerrors_pyに差分がない(self):
        for args in (["git", "diff", "--exit-code", "HEAD", "--", "notes/db.py", "notes/errors.py"],
                     ["git", "diff", "--cached", "--exit-code", "--", "notes/db.py", "notes/errors.py"]):
            proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "notes/db.py", "notes/errors.py"],
                                 cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(tracked.returncode, 0, "db.py / errors.py は git の追跡対象のはず")


if __name__ == "__main__":
    unittest.main()
