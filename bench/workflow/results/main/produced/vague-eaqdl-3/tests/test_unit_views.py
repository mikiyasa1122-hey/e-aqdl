"""V20: 閲覧数。"""
from notes import service
from notes.errors import NotFound
from tests.helpers import NotesTestCase


class ViewsTest(NotesTestCase):
    def test_views_閲覧のたびに1増え増加後の値を返す(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual([service.get_note(self.conn, self.alice, nid)["views"] for _ in range(3)], [1, 2, 3])
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], 4)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 4)

    def test_views_見えない閲覧は数えない(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b", private=True)
        for user in (self.bob, self.carol, self.dave):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, user, nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)

    def test_views_全列とversionは変わらない(self) -> None:
        nid = service.create_note(self.conn, self.alice, "題", "本", tags=["b", "a"])
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(set(got), {"id", "org_id", "owner_id", "title", "body", "private", "version",
                                    "views", "updated_at", "tags"})
        self.assertEqual((got["version"], got["tags"], got["private"]), (1, ["a", "b"], 0))

    def test_views_フック中に削除されたらNotFound(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.set_hook(lambda step: service.delete_note(self.conn, self.alice, nid) if step == "view" else None)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)
