"""ファイルDB（別接続が開ける）を使うテストの共通部分。"""
import tempfile
import unittest
from pathlib import Path

from notes import db, service


class FileDbBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "notes.db")
        self.conn = db.connect(self.path)
        self.alice = db.add_user(self.conn, 1, 10, "alice", api_key="ak-alice-0001")
        self.admin = db.add_user(self.conn, 3, 10, "root", role="admin", api_key="ak-root-0003")
        service.hooks.before_write = lambda step: None
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])

    def tearDown(self):
        service.hooks.before_write = lambda step: None
        self.conn.close()
        self._tmp.cleanup()

    def state(self, conn=None):
        row = (conn or self.conn).execute("SELECT version, archived FROM notes WHERE id = ?", (self.nid,)).fetchone()
        return (row["version"], row["archived"])

    def audits(self, action=None):
        if action:
            return [dict(r) for r in self.conn.execute("SELECT * FROM audit WHERE action = ?", (action,))]
        return [dict(r) for r in self.conn.execute("SELECT * FROM audit ORDER BY id")]

    def fail_at(self, wanted):
        def boom(step):
            if step == wanted:
                raise RuntimeError(f"injected failure at {step}")
        service.hooks.before_write = boom
