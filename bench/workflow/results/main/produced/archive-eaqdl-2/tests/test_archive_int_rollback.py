"""R12・AC13・V16: 書き込み途中の失敗で、状態も監査ログも残らない。"""
from notes import service

from .base import Base


class ArchiveRollbackTest(Base):
    def snapshot(self):
        return ([tuple(r) for r in self.conn.execute("SELECT * FROM notes ORDER BY id")],
                [tuple(r) for r in self.conn.execute("SELECT * FROM audit ORDER BY id")])

    def test_failure_after_update_rolls_back(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        for step, fn, prep in (("archive:audit", service.archive_note, False), ("unarchive:audit", service.unarchive_note, True)):
            with self.subTest(step=step):
                if prep:
                    service.archive_note(self.conn, self.alice, nid)
                before = self.snapshot()
                self.fail_at(step)
                with self.assertRaises(RuntimeError):
                    fn(self.conn, self.alice, nid)
                service.hooks.before_write = lambda s: None
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(self.snapshot(), before)
                # 接続を再利用できる
                self.assertEqual(service.get_note(self.conn, self.alice, nid)["archived"], 1 if prep else 0)

    def test_failure_before_transaction_leaves_nothing(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        before = self.snapshot()
        self.fail_at("archive")
        with self.assertRaises(RuntimeError):
            service.archive_note(self.conn, self.alice, nid)
        service.hooks.before_write = lambda s: None
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(service.archive_note(self.conn, self.alice, nid), 2)
