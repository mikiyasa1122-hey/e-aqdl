"""AC13・AC14（結合）: 別接続による競合・監査の失敗注入・ロールバック。一時ファイルDBのみ使う。"""
import os
import sqlite3
import tempfile
import unittest

from notes import db, service
from notes.errors import Conflict

_nop = lambda step: None


class FileDbBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.path)
        self.other = db.connect(self.path)
        self.alice = db.add_user(self.conn, 1, 10, "alice", api_key="ak-alice-0001")
        self.admin = db.add_user(self.conn, 3, 10, "root", role="admin", api_key="ak-root-0003")
        self.bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        service.hooks.before_write = _nop

    def tearDown(self):
        service.hooks.before_write = _nop
        self.conn.close()
        self.other.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def row(self, nid):
        return dict(service._row(self.conn, nid))

    def interrupt(self, wanted, fn):
        """hooks の指定の step で fn を1回だけ実行する（sleep なしで競合を再現）。"""
        done = []

        def hook(step):
            if step == wanted and not done:
                done.append(1)
                fn()
        service.hooks.before_write = hook


class ConcurrentArchiveTest(FileDbBase):
    def test_v5_concurrent_archive_only_one_succeeds(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        base_audit = self.count("audit")
        self.interrupt("archive", lambda: service.archive_note(self.other, self.admin, nid))
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, nid)
        r = self.row(nid)
        self.assertEqual((r["version"], r["archived"]), (2, 1))
        self.assertEqual(self.count("audit"), base_audit + 1)
        self.assertFalse(self.conn.in_transaction)

    def test_v5_concurrent_unarchive_only_one_succeeds(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, nid)
        base_audit = self.count("audit")
        self.interrupt("unarchive", lambda: service.unarchive_note(self.other, self.admin, nid))
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, nid)
        r = self.row(nid)
        self.assertEqual((r["version"], r["archived"]), (3, 0))
        self.assertEqual(self.count("audit"), base_audit + 1)

    def test_v13_archive_between_check_and_update_conflicts(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.interrupt("update", lambda: service.archive_note(self.other, self.alice, nid))
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, body="new")  # 呼び出し時点では最新の version
        r = self.row(nid)
        self.assertEqual((r["body"], r["version"], r["archived"]), ("b", 2, 1))
        self.assertFalse(self.conn.in_transaction)

    def test_v13_unauthorized_update_of_archived_gets_authz_error(self):
        from notes.errors import Forbidden
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, nid)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 2, body="x")


class AuditFailureTest(FileDbBase):
    def inject(self, action):
        self.conn.execute(
            f"CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON audit WHEN NEW.action = '{action}' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END")

    def test_v6_audit_failure_rolls_back_archive(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        base = self.count("audit")
        self.inject("archive")
        with self.assertRaises(sqlite3.DatabaseError):
            service.archive_note(self.conn, self.alice, nid)
        r = self.row(nid)
        self.assertEqual((r["version"], r["archived"]), (1, 0))
        self.assertEqual(self.count("audit"), base)

    def test_v6_audit_failure_rolls_back_unarchive(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, nid)
        base = self.count("audit")
        self.inject("unarchive")
        with self.assertRaises(sqlite3.DatabaseError):
            service.unarchive_note(self.conn, self.alice, nid)
        r = self.row(nid)
        self.assertEqual((r["version"], r["archived"]), (2, 1))
        self.assertEqual(self.count("audit"), base)

    def test_v7_connection_usable_after_failure(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.inject("archive")
        with self.assertRaises(sqlite3.DatabaseError):
            service.archive_note(self.conn, self.alice, nid)
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute("DROP TRIGGER fail_audit")
        self.assertEqual(service.archive_note(self.conn, self.alice, nid), 2)
        self.assertEqual(self.row(nid)["archived"], 1)

    def test_v7_hook_failure_rolls_back_and_recovers(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def boom(step):
            if step == "archive":
                raise RuntimeError("injected")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.archive_note(self.conn, self.alice, nid)
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = _nop
        self.assertEqual(service.archive_note(self.conn, self.alice, nid), 2)
