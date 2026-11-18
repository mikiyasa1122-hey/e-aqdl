"""V10 / V22: R10 例外後のロールバックと接続の再利用。"""
import os
import sqlite3
import tempfile

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.support import BaseCase, count


class RollbackTest(BaseCase):
    def assert_reusable(self, conn=None, user=None):
        conn = conn or self.conn
        user = user or self.u["alice"]
        self.assertFalse(conn.in_transaction)
        self.set_hook(lambda s: None)
        nid = service.create_note(conn, user, "続き", "b")
        self.assertEqual(service.get_note(conn, user, nid)["views"], 1)
        self.assertEqual(service.list_notes(conn, user)["total"] >= 1, True)

    def make(self, **kw):
        return service.create_note(self.conn, self.u["alice"], "題", "本", **kw)

    def test_unit_rollback_createの途中失敗(self):
        self.fail_at("create:tags")
        with self.assertRaises(RuntimeError):
            self.make(tags=["a"])
        self.assertEqual(count(self.conn, "notes"), 0)
        self.assert_reusable()

    def test_unit_rollback_updateの途中失敗(self):
        nid = self.make()
        # update の hook はトランザクション外なので、tx 内で失敗させるため audit を壊す
        self.conn.execute("CREATE TRIGGER boom BEFORE INSERT ON audit WHEN NEW.action = 'update'"
                          " BEGIN SELECT RAISE(ABORT, 'trigger boom'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "trigger boom"):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="新")
        self.assertEqual(self.conn.execute("SELECT title, version FROM notes").fetchone()[:], ("題", 1))
        self.conn.execute("DROP TRIGGER boom")
        self.assert_reusable()

    def test_unit_rollback_deleteの途中失敗(self):
        nid = self.make(tags=["a"])
        self.fail_at("delete:note")
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.u["alice"], nid)
        self.assertEqual(count(self.conn, "note_tags"), 1)
        self.assert_reusable()

    def test_unit_rollback_transferの途中失敗(self):
        nid = self.make()
        self.fail_at(f"transfer:{nid}")
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assertEqual(self.conn.execute("SELECT owner_id FROM notes").fetchone()[0], 1)
        self.assert_reusable()

    def test_unit_rollback_検証エラー_Conflict_Forbidden_NotFoundの後(self):
        nid = self.make()
        checks = [
            (ValidationError, lambda: self.make(tags=["NG"])),
            (Conflict, lambda: service.update_note(self.conn, self.u["alice"], nid, 5, title="x")),
            (Forbidden, lambda: service.update_note(self.conn, self.u["bob"], nid, 1, title="x")),
            (Forbidden, lambda: service.export_audit(self.conn, self.u["bob"])),
            (NotFound, lambda: service.get_note(self.conn, self.u["carol"], nid)),
            (NotFound, lambda: service.delete_note(self.conn, self.u["alice"], 999)),
            (ValidationError, lambda: service.list_notes(self.conn, self.u["alice"], page=0)),
        ]
        for exc, fn in checks:
            with self.assertRaises(exc):
                fn()
            self.assertFalse(self.conn.in_transaction)
        self.assert_reusable()

    def test_unit_rollback_閲覧の割り込み失敗でも接続は使える(self):
        nid = self.make()
        self.fail_at("view")
        with self.assertRaises(RuntimeError):
            service.get_note(self.conn, self.u["alice"], nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)
        self.assert_reusable()

    def test_unit_rollback_ファイルDBで失敗後に別接続がロック待ちなしに書ける(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "n.db")
            c1 = db.connect(path)
            c2 = db.connect(path)
            self.addCleanup(c1.close)
            self.addCleanup(c2.close)
            c2.execute("PRAGMA busy_timeout = 0")  # ロックが残っていれば即失敗する
            a = db.add_user(c1, 1, 1, "alice")
            self.fail_at("create:tags")
            with self.assertRaises(RuntimeError):
                service.create_note(c1, a, "t", "b", ["x"])
            self.assertFalse(c1.in_transaction)
            self.set_hook(lambda s: None)
            c2.execute("BEGIN IMMEDIATE")
            c2.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (1, 1, 'x', 'y')")
            c2.execute("COMMIT")
            self.assert_reusable(c1, a)
