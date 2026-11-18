"""V1 V2: 組織分離。"""
from notes import service
from notes.errors import NotFound
from tests.helpers import NotesTestCase


class IsolationTest(NotesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "組織1の題名", "組織1の本文", tags=["t1"])
        self.pid = service.create_note(self.conn, self.alice, "組織1の非公開", "本文", private=True)
        self.other = service.create_note(self.conn, self.dave, "組織2の題名", "組織2の本文", tags=["t2"])

    def test_isolation_全操作で他組織が見えない(self) -> None:
        for user in (self.dave, self.erin):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, user, self.nid)
            with self.assertRaises(NotFound):
                service.update_note(self.conn, user, self.nid, 1, title="x")
            with self.assertRaises(NotFound):
                service.delete_note(self.conn, user, self.nid)
        # 他組織の操作でメモは変化していない
        got = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((got["title"], got["version"]), ("組織1の題名", 1))

    def test_isolation_一覧検索totalに他組織が出ない(self) -> None:
        for user, own_ids in ((self.dave, [self.other]), (self.erin, [self.other]),
                              (self.alice, [self.nid, self.pid]), (self.bob, [self.nid]),
                              (self.carol, [self.nid])):
            listed = service.list_notes(self.conn, user)
            self.assertEqual(sorted(i["id"] for i in listed["items"]), sorted(own_ids))
            self.assertEqual(listed["total"], len(own_ids))
            found = service.search_notes(self.conn, user, "")
            self.assertEqual([i["id"] for i in found], sorted(own_ids))

    def test_isolation_他組織のタグと検索語でも見えない(self) -> None:
        # V2: 他組織の id・タグ・検索語を指定する
        self.assertEqual(service.search_notes(self.conn, self.dave, "組織1"), [])
        self.assertEqual(service.search_notes(self.conn, self.dave, "", tags=["t1"]), [])
        self.assertEqual(service.search_notes(self.conn, self.alice, "組織2"), [])
        self.assertEqual(service.search_notes(self.conn, self.alice, "", tags=["t2"]), [])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.dave, "", tags=["t2"])],
                         [self.other])

    def test_isolation_NotFoundは存在しないidと区別できない(self) -> None:
        with self.assertRaises(NotFound) as cross:
            service.get_note(self.conn, self.dave, self.nid)
        with self.assertRaises(NotFound) as missing:
            service.get_note(self.conn, self.dave, 99999)
        self.assertEqual(str(cross.exception), str(missing.exception))

    def test_isolation_他組織の操作は監査に残らず削除も効かない(self) -> None:
        before = self.snapshot()
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.erin, self.nid)
        self.assertEqual(self.snapshot(), before)
