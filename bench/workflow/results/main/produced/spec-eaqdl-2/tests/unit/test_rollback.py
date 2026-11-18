"""V23, V24: 例外後にトランザクションが残らず、同じ接続で次の操作が成功する。"""
import sqlite3

from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import BaseCase


class FailureThenReuseTest(BaseCase):
    """V23: 各操作の失敗（業務エラー）の後。"""

    def test_各操作の業務エラー後(self) -> None:
        nid = self.make(self.alice)
        priv = self.make(self.alice, private=True)
        failures = [
            (ValidationError, lambda: service.create_note(self.conn, self.alice, "", "b")),
            (NotFound, lambda: service.get_note(self.conn, self.bob, priv)),
            (ValidationError, lambda: service.list_notes(self.conn, self.alice, page=0)),
            (ValidationError, lambda: service.search_notes(self.conn, self.alice, "")),
            (Conflict, lambda: service.update_note(self.conn, self.alice, nid, 9, title="x")),
            (Forbidden, lambda: service.update_note(self.conn, self.bob, nid, 1, title="x")),
            (ValidationError, lambda: service.update_note(self.conn, self.alice, nid, 1, title="")),
            (Forbidden, lambda: service.delete_note(self.conn, self.bob, nid)),
            (NotFound, lambda: service.delete_note(self.conn, self.alice, 999)),
            (Forbidden, lambda: service.transfer_notes(self.conn, self.alice, self.alice, self.bob)),
            (Forbidden, lambda: service.export_audit(self.conn, self.alice)),
        ]
        for exc, fn in failures:
            with self.subTest(exc.__name__):
                with self.assertRaises(exc):
                    fn()
                self.assert_reusable()

    def test_SQLiteエラー後もロールバックされ再利用できる(self) -> None:
        # 存在しない移管先 id は外部キー違反（foreign_keys=ON）で落ちる。トランザクション内で発生する
        ghost = {"id": 999, "org_id": 1, "name": "ghost", "role": "member", "api_key": "k"}
        self.make(self.alice)
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            service.transfer_notes(self.conn, self.admin, self.alice, ghost)
        self.assertEqual(self.snapshot(), before)
        self.assert_reusable()


class HookFailureThenReuseTest(BaseCase):
    """V24: フック失敗の各操作の後。"""

    def test_create_tags(self) -> None:
        self.fail_at("create:tags")
        with self.assertRaises(RuntimeError):
            self.make(self.alice, tags=["a"])
        self.assertFalse(self.conn.in_transaction)
        self._restore_and_check()

    def test_view(self) -> None:
        nid = self.make(self.alice)
        self.fail_at("view")
        with self.assertRaises(RuntimeError):
            service.get_note(self.conn, self.alice, nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)
        self._restore_and_check()

    def test_update(self) -> None:
        nid = self.make(self.alice)
        self.fail_at("update")
        with self.assertRaises(RuntimeError):
            service.update_note(self.conn, self.alice, nid, 1, title="x")
        self.assertEqual(self.conn.execute("SELECT version FROM notes").fetchone()[0], 1)
        self._restore_and_check()

    def test_delete_note(self) -> None:
        nid = self.make(self.alice, tags=["a"])
        self.fail_at("delete:note")
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(self.counts()["note_tags"], 1)
        self._restore_and_check()

    def test_transfer(self) -> None:
        nid = self.make(self.alice)
        self.fail_at(f"transfer:{nid}")
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.conn.execute("SELECT owner_id FROM notes").fetchone()[0], 2)
        self._restore_and_check()

    def _restore_and_check(self) -> None:
        self.assertFalse(self.conn.in_transaction)
        self.restore_hook()  # フックを元に戻した後、同じ接続で各操作が成功する
        nid = self.make(self.bob, title="次の操作")
        self.assertEqual(service.update_note(self.conn, self.bob, nid, 1, title="次の更新"), 2)
        service.delete_note(self.conn, self.bob, nid)
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.bob, self.alice), 0)
        self.assertFalse(self.conn.in_transaction)
