"""AC10・K4: 例外が起きたらロールバックし、接続を次の操作に使い続けられる。"""
import sqlite3

from notes import service
from notes.errors import Conflict, ValidationError

from .base import Base


class RecoveryTest(Base):
    def test_connection_usable_after_each_kind_of_failure(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        failures = [
            (Conflict, lambda: service.update_note(self.conn, self.alice, nid, 9, body="x")),
            (ValidationError, lambda: service.update_note(self.conn, self.alice, nid, 1, tags=["BAD"])),
        ]
        for exc, call in failures:
            with self.subTest(exc=exc.__name__):
                with self.assertRaises(exc):
                    call()
                self.assertFalse(self.conn.in_transaction)
                service.create_note(self.conn, self.alice, "next", "ok")
        self.fail_at("create:tags")
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b")
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = lambda s: None
        self.assertEqual(service.update_note(self.conn, self.alice, nid, 1, body="after"), 2)

    def test_database_error_is_reported_and_rolled_back(self):
        def break_schema(step):
            if step == "create:tags":
                self.conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (999999, "x"))  # 外部キー違反
        service.hooks.before_write = break_schema
        with self.assertRaises(sqlite3.IntegrityError):
            service.create_note(self.conn, self.alice, "t", "b")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.count("notes"), 0)

    def test_original_error_is_not_hidden_when_rollback_is_impossible(self):
        """SQLite が自動でロールバックした後でも、元の例外がそのまま伝わる。"""
        class Boom(Exception):
            pass

        def end_txn(step):
            if step == "create:tags":
                self.conn.execute("ROLLBACK")  # トランザクションがもう開いていない状態を作る
                raise Boom()
        service.hooks.before_write = end_txn
        with self.assertRaises(Boom):
            service.create_note(self.conn, self.alice, "t", "b")
        service.hooks.before_write = lambda s: None
        service.create_note(self.conn, self.alice, "ok", "b")
