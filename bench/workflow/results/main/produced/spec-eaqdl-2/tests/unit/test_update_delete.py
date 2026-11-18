"""V10, V12(delete): 更新・削除の権限・Conflict・検証・原子性。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import BaseCase


class UpdateTest(BaseCase):
    def test_作成者は更新でき_versionが増え_Noneは変えない(self) -> None:
        nid = self.make(self.alice, title="旧題名", body="旧本文", tags=["a"])
        v = service.update_note(self.conn, self.alice, nid, 1, title="新題名")
        self.assertEqual(v, 2)
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((row["title"], row["body"], row["version"], row["tags"]), ("新題名", "旧本文", 2, ["a"]))
        v = service.update_note(self.conn, self.alice, nid, 2, body="新本文", tags=["b", "c", "b"])
        self.assertEqual(v, 3)
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((row["title"], row["body"], row["tags"]), ("新題名", "新本文", ["b", "c"]))
        service.update_note(self.conn, self.alice, nid, 3, tags=[])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], [])

    def test_同組織管理者は更新と削除ができる(self) -> None:
        nid = self.make(self.alice)
        self.assertEqual(service.update_note(self.conn, self.admin, nid, 1, title="管理者が更新"), 2)
        service.delete_note(self.conn, self.admin, nid)
        self.assertEqual(self.counts()["notes"], 0)

    def test_他のmemberはForbidden(self) -> None:
        nid = self.make(self.alice)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, nid)
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((row["title"], row["version"]), ("題名", 1))

    def test_他組織の管理者はNotFound(self) -> None:
        nid = self.make(self.alice)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.admin2, nid, 1, title="x")

    def test_古いversionはConflictで変更されない(self) -> None:
        nid = self.make(self.alice)
        service.update_note(self.conn, self.alice, nid, 1, title="新")
        for v in (1, 0, 3):
            with self.subTest(v), self.assertRaises(Conflict):
                service.update_note(self.conn, self.alice, nid, v, title="古い")
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((row["title"], row["version"]), ("新", 2))

    def test_更新でもR1の検証(self) -> None:
        nid = self.make(self.alice, tags=["keep"])
        for name, kw in [("題名空", {"title": ""}), ("題名空白", {"title": "  "}), ("題名101", {"title": "a" * 101}),
                         ("本文10001", {"body": "x" * 10001}), ("タグ6個", {"tags": list("abcdef")}),
                         ("タグ大文字", {"tags": ["A"]}), ("タグ文字列単体", {"tags": "abc"})]:
            with self.subTest(name), self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, nid, 1, **kw)  # type: ignore[arg-type]
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((row["version"], row["tags"]), (1, ["keep"]))

    def test_検証はversion確認より前(self) -> None:
        nid = self.make(self.alice)
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.alice, nid, 99, title="")

    def test_監査ログ(self) -> None:
        nid = self.make(self.alice)
        service.update_note(self.conn, self.admin, nid, 1, title="x")
        r = self.conn.execute("SELECT actor_id, action, detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(r), (1, "update", f"note={nid}"))

    def test_updateフックで同接続の割り込みがあればConflict(self) -> None:
        nid = self.make(self.alice)

        def hook(step: str) -> None:
            if step == "update":
                self.conn.execute("UPDATE notes SET version = version + 1, title = '先の更新' WHERE id = ?", (nid,))
        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, title="後の更新")
        self.assertEqual(self.conn.execute("SELECT title FROM notes").fetchone()[0], "先の更新")
        self.assertFalse(self.conn.in_transaction)


class DeleteTest(BaseCase):
    def test_削除でタグとメモが消え監査が残る(self) -> None:
        nid = self.make(self.alice, tags=["a", "b"])
        service.delete_note(self.conn, self.alice, nid)
        c = self.counts()
        self.assertEqual((c["notes"], c["note_tags"]), (0, 0))
        r = self.conn.execute("SELECT action, detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(r), ("delete", f"note={nid}"))
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)

    def test_不存在はNotFound(self) -> None:
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.alice, 999)

    def test_delete_note失敗でタグもメモも残り例外が伝わる(self) -> None:
        nid = self.make(self.alice, tags=["a", "b"])
        before = self.counts()
        self.fail_at("delete:note")
        with self.assertRaisesRegex(RuntimeError, "意図した失敗"):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 2)
        self.assertFalse(self.conn.in_transaction)
