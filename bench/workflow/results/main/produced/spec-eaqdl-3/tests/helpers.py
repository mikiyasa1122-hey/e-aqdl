"""テスト共通: 一時ディレクトリのファイルDB・利用者・フックの復元。"""
import os
import tempfile
import unittest

from notes import db, service


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self._orig_hook = service.hooks.before_write
        self.addCleanup(self._restore_hook)
        self.conn = self.new_conn()
        self.alice = db.add_user(self.conn, 1, 1, "alice", "member", "alice-key-0001")
        self.bob = db.add_user(self.conn, 2, 1, "bob", "member", "bob-key-00002")
        self.admin = db.add_user(self.conn, 3, 1, "admin", "admin", "admin-key-0003")
        self.admin2 = db.add_user(self.conn, 4, 1, "admin2", "admin", "adm2-key-0004")
        self.eve = db.add_user(self.conn, 5, 2, "eve", "member", "eve-key-00005")
        self.eve_admin = db.add_user(self.conn, 6, 2, "eveadm", "admin", "eveadm-key-0006")

    def _restore_hook(self):
        service.hooks.before_write = self._orig_hook

    def new_conn(self):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def set_hook(self, fn):
        service.hooks.before_write = fn

    def count(self, table, conn=None):
        return (conn or self.conn).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def counts(self):
        return {t: self.count(t) for t in ("notes", "note_tags", "audit")}

    def note_row(self, note_id):
        r = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return dict(r) if r else None
