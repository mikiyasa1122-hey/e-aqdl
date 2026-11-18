"""V3 V4: 非公開メモ。"""
from notes import service
from notes.errors import NotFound
from tests.helpers import NotesTestCase


class PrivateTest(NotesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pub = service.create_note(self.conn, self.alice, "公開", "本文")
        self.priv = service.create_note(self.conn, self.alice, "非公開", "本文", private=True)

    def test_private_作成者だけが見える(self) -> None:
        self.assertEqual(service.get_note(self.conn, self.alice, self.priv)["private"], 1)
        listed = service.list_notes(self.conn, self.alice)
        self.assertEqual(listed["total"], 2)
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "非公開")], [self.priv])

    def test_private_同組織のmemberと管理者には見えない(self) -> None:
        for user in (self.bob, self.carol):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, user, self.priv)
            listed = service.list_notes(self.conn, user)
            self.assertEqual([i["id"] for i in listed["items"]], [self.pub])
            self.assertEqual(listed["total"], 1)
            self.assertEqual(service.search_notes(self.conn, user, "非公開"), [])
            self.assertEqual([i["id"] for i in service.search_notes(self.conn, user, "")], [self.pub])

    def test_private_管理者でも更新削除できずNotFound(self) -> None:
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.carol, self.priv, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.carol, self.priv)

    def test_private_transfer後も非公開のまま新所有者にだけ見える(self) -> None:
        # V4: 引き継ぎで非公開メモの設定は変わらない
        count = service.transfer_notes(self.conn, self.carol, self.alice, self.bob)
        self.assertEqual(count, 2)
        got = service.get_note(self.conn, self.bob, self.priv)
        self.assertEqual((got["private"], got["owner_id"]), (1, self.bob["id"]))
        for user in (self.alice, self.carol):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, user, self.priv)
            self.assertNotIn(self.priv, [i["id"] for i in service.list_notes(self.conn, user)["items"]])
            self.assertNotIn(self.priv, [i["id"] for i in service.search_notes(self.conn, user, "")])
