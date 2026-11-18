"""V8 V9: 更新・削除の権限と監査。"""
from notes import service
from notes.errors import Forbidden, NotFound
from tests.helpers import NotesTestCase


class PermissionTest(NotesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "題", "本", tags=["a"])

    def _audit(self) -> list[tuple]:
        return [(r["action"], r["detail"], r["actor_id"]) for r in
                self.conn.execute("SELECT * FROM audit ORDER BY id")]

    def test_update_作成者と同組織管理者は可(self) -> None:
        self.assertEqual(service.update_note(self.conn, self.alice, self.nid, 1, title="一"), 2)
        self.assertEqual(service.update_note(self.conn, self.carol, self.nid, 2, title="二"), 3)
        self.assertEqual(self._audit(), [("create", f"note={self.nid}", 1), ("update", f"note={self.nid}", 1),
                                         ("update", f"note={self.nid}", 3)])

    def test_update_別memberは公開メモでもForbidden(self) -> None:
        before = self.snapshot()
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, self.nid, 1, title="x")
        self.assertEqual(self.snapshot(), before)

    def test_update_他組織管理者はNotFound(self) -> None:
        before = self.snapshot()
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.erin, self.nid, 1, title="x")
        self.assertEqual(self.snapshot(), before)

    def test_delete_作成者は可でタグも消え監査が残る(self) -> None:
        service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        self.assertEqual(self._audit()[-1], ("delete", f"note={self.nid}", 1))

    def test_delete_同組織管理者は可(self) -> None:
        service.delete_note(self.conn, self.carol, self.nid)
        self.assertEqual(self._audit()[-1], ("delete", f"note={self.nid}", 3))

    def test_delete_別memberはForbiddenで他組織管理者はNotFound(self) -> None:
        before = self.snapshot()
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, self.nid)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.erin, self.nid)
        self.assertEqual(self.snapshot(), before)

    def test_delete_存在しないidはNotFound(self) -> None:
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.alice, 9999)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, 9999, 1, title="x")

    def test_update_None項目は不変でtagsは置換(self) -> None:
        service.update_note(self.conn, self.alice, self.nid, 1, tags=["b", "c"])
        got = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((got["title"], got["body"], got["tags"], got["version"]), ("題", "本", ["b", "c"], 2))
        service.update_note(self.conn, self.alice, self.nid, 2, title="新", body="新本")
        got = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((got["title"], got["body"], got["tags"], got["version"]), ("新", "新本", ["b", "c"], 3))
        service.update_note(self.conn, self.alice, self.nid, 3, tags=[])
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["tags"], [])

    def test_update_updated_atが更新される(self) -> None:
        service._now = lambda: 5000
        service.update_note(self.conn, self.alice, self.nid, 1, title="x")
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["updated_at"], 5000)
