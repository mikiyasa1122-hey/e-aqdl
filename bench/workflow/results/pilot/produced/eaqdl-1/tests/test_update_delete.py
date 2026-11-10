from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import DbTestCase, FileDbTestCase, snapshot


def _row(conn, nid):
    return conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()


class UpdateTest(DbTestCase):
    def test_update_成功でversionが増えNoneは変えない(self):
        nid = self.note(title="旧", body="旧本文", tags=("a",))
        v = service.update_note(self.conn, self.u["alice"], nid, 1, title="新")
        self.assertEqual(v, 2)
        row = _row(self.conn, nid)
        self.assertEqual((row["title"], row["body"], row["version"]), ("新", "旧本文", 2))
        tags = [r[0] for r in self.conn.execute("SELECT tag FROM note_tags WHERE note_id=?", (nid,))]
        self.assertEqual(tags, ["a"])
        self.assertEqual(self.conn.execute("SELECT detail FROM audit WHERE action='update'").fetchone()[0],
                         f"note={nid}")

    def test_update_タグは置き換え_空列で全削除(self):
        nid = self.note(tags=("a", "b"))
        service.update_note(self.conn, self.u["alice"], nid, 1, tags=("c", "c"))
        self.assertEqual(service.get_note(self.conn, self.u["alice"], nid)["tags"], ["c"])
        service.update_note(self.conn, self.u["alice"], nid, 2, tags=())
        self.assertEqual(service.get_note(self.conn, self.u["alice"], nid)["tags"], [])

    def test_update_入力検証は更新前に拒否(self):
        nid = self.note(tags=("a",))
        before = snapshot(self.conn)
        for kw in ({"title": ""}, {"title": "  "}, {"title": "x" * 101}, {"body": "x" * 10001},
                   {"tags": ("BAD",)}, {"tags": "abc"}, {"tags": ("a",) * 6}):
            with self.assertRaises(ValidationError, msg=repr(kw)):
                service.update_note(self.conn, self.u["alice"], nid, 1, **kw)
        self.assertEqual(snapshot(self.conn), before)

    def test_update_権限(self):
        pub = self.note()
        priv = self.note(private=True)
        # 同じ組織の他の一般利用者は Forbidden、管理者は更新できる
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.u["bob"], pub, 1, title="x")
        self.assertEqual(service.update_note(self.conn, self.u["admin"], pub, 1, title="x"), 2)
        # 非公開は管理者にも見えない（NotFound）。他組織も NotFound
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["admin"], priv, 1, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["root2"], pub, 2, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["alice"], 9999, 1, title="x")
        self.assertEqual(_row(self.conn, priv)["version"], 1)

    def test_update_version不一致はConflictで何も変えない(self):
        nid = self.note()
        for bad in (0, 2, -1):
            before = snapshot(self.conn)
            with self.assertRaises(Conflict):
                service.update_note(self.conn, self.u["alice"], nid, bad, title="x")
            self.assertEqual(snapshot(self.conn), before)

    def test_update_検証の順序は権限よりversionより前に可視性(self):
        priv = self.note(private=True)
        # 他人が古い version で非公開を更新しようとしても Conflict でなく NotFound
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["bob"], priv, 99, title="x")
        pub = self.note()
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.u["bob"], pub, 99, title="x")


class UpdateConcurrentTest(FileDbTestCase):
    def test_update_updateフックでの割り込み更新はConflict(self):
        nid = self.note(title="元")
        other = self.new_conn()
        state = {"done": False}

        def hook(step: str) -> None:
            if step == "update" and not state["done"]:
                state["done"] = True
                service.update_note(other, self.u["alice"], nid, 1, title="先の更新")
        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="後の更新")
        row = _row(self.conn, nid)
        self.assertEqual((row["title"], row["version"]), ("先の更新", 2))
        self.assertFalse(self.conn.in_transaction)

    def test_update_updateフック中にメモが削除されたらNotFound(self):
        nid = self.note()
        other = self.new_conn()

        def hook(step: str) -> None:
            if step == "update":
                service.delete_note(other, self.u["alice"], nid)
        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="x")

    def test_update_updateフック中の移管でも更新は失われずConflict(self):
        nid = self.note()
        other = self.new_conn()

        def hook(step: str) -> None:
            if step == "update":
                service.transfer_notes(other, self.u["admin"], self.u["alice"], self.u["bob"])
        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="x")
        row = _row(self.conn, nid)
        self.assertEqual((row["owner_id"], row["title"]), (2, "題名"))


class DeleteTest(DbTestCase):
    def test_delete_タグとメモを消し監査ログを残す(self):
        nid = self.note(tags=("a", "b"))
        self.assertIsNone(service.delete_note(self.conn, self.u["alice"], nid))
        self.assertIsNone(_row(self.conn, nid))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT detail FROM audit WHERE action='delete'").fetchone()[0],
                         f"note={nid}")

    def test_delete_権限(self):
        pub = self.note()
        priv = self.note(private=True)
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.u["bob"], pub)
        for who, nid in (("admin", priv), ("carol", pub), ("alice", 9999)):
            with self.assertRaises(NotFound, msg=who):
                service.delete_note(self.conn, self.u[who], nid)
        self.assertIsNotNone(_row(self.conn, pub))
        self.assertIsNotNone(_row(self.conn, priv))
        service.delete_note(self.conn, self.u["admin"], pub)  # 同組織の管理者は消せる
        self.assertIsNone(_row(self.conn, pub))

    def test_delete_delete_note失敗は伝わり何も消えない(self):
        nid = self.note(tags=("a", "b"))
        before = snapshot(self.conn)
        self.fail_at("delete:note", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.u["alice"], nid)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 2)
        self.assertFalse(self.conn.in_transaction)
