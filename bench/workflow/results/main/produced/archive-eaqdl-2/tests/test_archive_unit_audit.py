"""R12・AC13: 監査ログ。成功で1件、失敗では何も残らない。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound

from .base import Base


class ArchiveAuditTest(Base):
    def test_success_records_one_audit_entry_each(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        base = [a for a in service.export_audit(self.conn, self.admin)]
        service.archive_note(self.conn, self.admin, nid)
        service.unarchive_note(self.conn, self.alice, nid)
        new = service.export_audit(self.conn, self.admin)[len(base):]
        self.assertEqual([(a["action"], a["detail"], a["actor"]) for a in new],
                         [("archive", f"note={nid}", "root"), ("unarchive", f"note={nid}", "alice")])
        self.assertNotIn("ak-root-0003", str(new))

    def test_failures_leave_notes_and_audit_unchanged(self):
        pub = service.create_note(self.conn, self.alice, "pub", "b")
        priv = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        cases = [(NotFound, service.archive_note, self.eve, pub), (NotFound, service.archive_note, self.admin, priv),
                 (NotFound, service.archive_note, self.alice, 9999), (Forbidden, service.archive_note, self.bob, pub),
                 (Conflict, service.unarchive_note, self.alice, pub)]
        for exc, fn, actor, nid in cases:
            with self.subTest(exc=exc.__name__, nid=nid, actor=actor["name"]):
                notes = self.conn.execute("SELECT * FROM notes ORDER BY id").fetchall()
                audit_n = self.count("audit")
                with self.assertRaises(exc):
                    fn(self.conn, actor, nid)
                self.assertEqual([tuple(r) for r in notes],
                                 [tuple(r) for r in self.conn.execute("SELECT * FROM notes ORDER BY id")])
                self.assertEqual(audit_n, self.count("audit"))
                self.assertFalse(self.conn.in_transaction)
                service.get_note(self.conn, self.alice, pub)  # 同じ接続で続けられる
        service.archive_note(self.conn, self.alice, pub)
        n = self.count("audit")
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, pub)
        self.assertEqual(n, self.count("audit"))
