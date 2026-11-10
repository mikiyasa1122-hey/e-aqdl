from notes import service
from notes.errors import ValidationError
from tests.helpers import DbTestCase, snapshot


class Boom(BaseException):
    """Exception でない例外（KeyboardInterrupt 相当）。"""


class RollbackTest(DbTestCase):
    def assert_reusable(self):
        self.assertFalse(self.conn.in_transaction)
        self.reset_hook()
        nid = self.note(title="次の操作")
        self.assertEqual(service.get_note(self.conn, self.u["alice"], nid)["title"], "次の操作")
        self.assertFalse(self.conn.in_transaction)

    def test_create失敗後に同じ接続で次の操作が成功する(self):
        self.fail_at("create:tags", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            self.note(tags=("a",))
        self.assert_reusable()

    def test_delete失敗後に同じ接続で次の操作が成功する(self):
        nid = self.note()
        self.fail_at("delete:note", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.u["alice"], nid)
        self.assert_reusable()
        service.delete_note(self.conn, self.u["alice"], nid)  # 再試行で消せる

    def test_transfer失敗後に同じ接続で次の操作が成功する(self):
        a = self.note()
        self.fail_at(f"transfer:{a}", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assert_reusable()
        self.assertEqual(service.transfer_notes(self.conn, self.u["admin"], self.u["alice"],
                                                self.u["bob"]), 2)

    def test_Exceptionでない例外でもロールバックされる(self):
        before = snapshot(self.conn)
        self.fail_at("create:tags", Boom())
        with self.assertRaises(Boom):
            self.note()
        self.assertEqual(snapshot(self.conn), before)
        self.assert_reusable()

    def test_view更新とupdate途中の失敗でも接続が使える(self):
        nid = self.note()
        self.fail_at("view", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            service.get_note(self.conn, self.u["alice"], nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)
        self.fail_at("update", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="x")
        self.assert_reusable()

    def test_検証エラーでもトランザクションを残さない(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.u["alice"], "", "b")
        self.assert_reusable()

    def test_業務エラーの後も接続が使える(self):
        nid = self.note(private=True)
        with self.assertRaises(service.NotFound):
            service.get_note(self.conn, self.u["bob"], nid)
        with self.assertRaises(service.Conflict):
            service.update_note(self.conn, self.u["alice"], nid, 5, title="x")
        self.assert_reusable()

    def test_ROLLBACK自体が失敗しても元の例外が伝わる(self):
        # 失敗注入: hook 内で接続を閉じるとロールバックできないが、元の例外を隠さない
        import sqlite3
        conn2 = sqlite3.connect(":memory:", isolation_level=None)
        with self.assertRaises(ZeroDivisionError), service._tx(conn2):
            conn2.execute("COMMIT")  # 先にコミットして in_transaction を False に
            conn2.execute("BEGIN")
            conn2.close()
            raise ZeroDivisionError
