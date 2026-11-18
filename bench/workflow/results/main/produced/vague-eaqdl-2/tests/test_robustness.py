"""AC18 AC19 AC20 K3 K12: 堅牢性（ログ・例外メッセージ・標準ライブラリのみ・未変更ファイル）。"""
import ast
import logging
import os
import shutil
import subprocess
import sys
import unittest

from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import MemCase, hook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


BODY = "SECRET-BODY-xyz"
TITLE = "SECRET-TITLE-xyz"
TAG = "SECRET-TAG-xyz"


class RobustnessUnit(MemCase):
    def test_作成ログにidと作成者idが出て本文と鍵は出ない(self):  # V19
        with self.assertLogs("notes", level="INFO") as cm:
            nid = self.note(self.alice, TITLE, BODY, [TAG])
        self.assertEqual(len(cm.records), 1)
        rec = cm.records[0]
        self.assertEqual(rec.levelname, "INFO")
        self.assertEqual(rec.getMessage(), f"note created id={nid} owner={self.alice['id']}")
        text = "\n".join(cm.output)
        for secret in (BODY, TITLE, TAG, self.alice["api_key"]):
            self.assertNotIn(secret, text)

    def test_ロールバックしたら作成ログを出さない(self):  # V19
        def boom(step):
            raise RuntimeError("x")
        with self.assertLogs("notes", level="DEBUG") as cm:
            logging.getLogger("notes").debug("番兵")
            with hook(boom), self.assertRaises(RuntimeError):
                self.note()
        self.assertEqual(len(cm.records), 1)

    def test_全ログに本文と鍵が含まれない(self):  # V32
        with self.assertLogs("notes", level="DEBUG") as cm:
            nid = self.note(self.alice, TITLE, BODY, [TAG], private=True)
            service.get_note(self.conn, self.alice, nid)
            service.update_note(self.conn, self.alice, nid, 1, body=BODY + "2")
            service.search_notes(self.conn, self.alice, BODY)
            service.list_notes(self.conn, self.alice)
            service.transfer_notes(self.conn, self.root1, 1, 2)
            service.export_audit(self.conn, self.root1)
            service.delete_note(self.conn, self.bob, nid)
        text = "\n".join(cm.output)
        for secret in (BODY, TITLE, TAG, "test-key-"):
            self.assertNotIn(secret, text)

    def test_例外メッセージに本文と鍵が含まれない(self):  # V32
        nid = self.note(self.alice, TITLE, BODY, [TAG], private=True)
        attempts = [
            (NotFound, lambda: service.get_note(self.conn, self.bob, nid)),
            (Forbidden, lambda: service.export_audit(self.conn, self.alice)),
            (Conflict, lambda: service.update_note(self.conn, self.alice, nid, 9, title=TITLE)),
            (ValidationError, lambda: service.create_note(self.conn, self.alice, TITLE, BODY, [TAG * 3])),
            (ValidationError, lambda: service.create_note(self.conn, self.alice, TITLE, BODY * 2000)),
            (ValidationError, lambda: service.search_notes(self.conn, self.alice, TAG * 50)),
            (ValidationError, lambda: service.create_note(
                self.conn, {"id": 99, "org_id": 1, "role": "member", "api_key": "test-key-ghost"},
                TITLE, BODY)),
        ]
        for exc, fn in attempts:
            with self.subTest(exc=exc.__name__), self.assertRaises(exc) as cm:
                fn()
            msg = str(cm.exception)
            for secret in (BODY, TITLE, TAG, "test-key-"):
                self.assertNotIn(secret, msg)
            self.assertIsNone(cm.exception.__cause__)  # 元の sqlite3 例外を連鎖させない

    def test_SQL注入でテーブルが壊れない(self):  # V18
        evil = "'; DROP TABLE notes; DROP TABLE users; --"
        nid = self.note(self.alice, evil, evil, [evil[:32]])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, evil, [evil[:32]])), 1)
        service.update_note(self.conn, self.alice, nid, 1, title=evil + "2", tags=[evil[:32]])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["title"], evil + "2")
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "notes", "note_tags", "audit"} <= names)


class RobustnessIntegration(unittest.TestCase):
    def test_notesは標準ライブラリだけをimportする(self):  # V20
        stdlib = set(sys.stdlib_module_names)
        pkg = os.path.join(ROOT, "notes")
        found = 0
        for name in os.listdir(pkg):
            if not name.endswith(".py"):
                continue
            found += 1
            tree = ast.parse(_read(os.path.join(pkg, name)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [(node.module or "").split(".")[0]]
                else:
                    continue
                for m in mods:
                    self.assertIn(m, stdlib, f"{name} が標準ライブラリ以外を import")
        self.assertGreaterEqual(found, 6)

    def test_testsも標準ライブラリとnotesだけをimportする(self):  # V20
        stdlib = set(sys.stdlib_module_names) | {"notes", "tests"}
        tdir = os.path.join(ROOT, "tests")
        for name in os.listdir(tdir):
            if name.endswith(".py"):
                tree = ast.parse(_read(os.path.join(tdir, name)))
                for node in ast.walk(tree):
                    mods = []
                    if isinstance(node, ast.Import):
                        mods = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        mods = [(node.module or "").split(".")[0]]
                    for m in mods:
                        self.assertIn(m, stdlib, f"{name}: {m}")

    def test_db_pyとerrors_pyは未変更(self):  # V20
        git = shutil.which("git")
        if git is None or not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("git が使えない環境")
        files = ["notes/db.py", "notes/errors.py"]
        diff = subprocess.run([git, "diff", "--exit-code", "HEAD", "--"] + files, cwd=ROOT,
                              capture_output=True, text=True, check=False)
        self.assertEqual(diff.returncode, 0, diff.stdout)
        for f in files:  # 履歴上も初期コミットから変わっていない
            log = subprocess.run([git, "log", "--format=%H", "--", f], cwd=ROOT,
                                 capture_output=True, text=True, check=False)
            self.assertLessEqual(len(log.stdout.split()), 1, f)
