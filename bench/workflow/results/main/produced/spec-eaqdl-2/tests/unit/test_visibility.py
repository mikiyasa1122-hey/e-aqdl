"""V9: 他組織・他人の非公開が、閲覧・一覧・検索・更新・削除・移管で見えない/操作できない。"""
from notes import service
from notes.errors import Forbidden, NotFound
from tests.helpers import BaseCase


class VisibilityTest(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.pub = self.make(self.alice, title="公開ゼータ", body="本文ゼータ")
        self.priv = self.make(self.alice, title="非公開ゼータ", body="本文ゼータ", private=True)

    def hidden(self):  # 閲覧できない (名前, 利用者, id)
        return [("他組織", self.carol, self.pub), ("他組織管理者", self.admin2, self.pub),
                ("他人の非公開(member)", self.bob, self.priv),
                ("他人の非公開(同組織管理者)", self.admin, self.priv),
                ("他組織管理者の非公開", self.admin2, self.priv)]

    def test_get(self) -> None:
        for name, user, nid in self.hidden():
            with self.subTest(name), self.assertRaises(NotFound):
                service.get_note(self.conn, user, nid)

    def test_update_delete(self) -> None:
        for name, user, nid in self.hidden():
            with self.subTest(name):
                with self.assertRaises(NotFound):
                    service.update_note(self.conn, user, nid, 1, title="x")
                with self.assertRaises(NotFound):
                    service.delete_note(self.conn, user, nid)
        self.assertEqual(self.counts()["notes"], 2)
        self.assertEqual(service.get_note(self.conn, self.alice, self.pub)["version"], 1)

    def test_一覧と検索に出ない(self) -> None:
        def ids(user):
            return {i["id"] for i in service.list_notes(self.conn, user)["items"]}

        def found(user):
            return {i["id"] for i in service.search_notes(self.conn, user, "ゼータ")}
        self.assertEqual(ids(self.alice), {self.pub, self.priv})
        self.assertEqual(found(self.alice), {self.pub, self.priv})
        for user in (self.bob, self.admin):
            self.assertEqual(ids(user), {self.pub})
            self.assertEqual(found(user), {self.pub})
            self.assertEqual(service.list_notes(self.conn, user)["total"], 1)
        for user in (self.carol, self.admin2):
            self.assertEqual(ids(user), set())
            self.assertEqual(found(user), set())
            self.assertEqual(service.list_notes(self.conn, user)["total"], 0)

    def test_移管は他組織のメモを動かさない(self) -> None:
        other = self.make(self.carol, title="他組織")
        # 組織違いの元/先は Forbidden（DB に触れない）
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.admin, self.carol, self.alice)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.admin, self.alice, self.carol)
        self.assertEqual(self.conn.execute("SELECT owner_id FROM notes WHERE id = ?", (other,)).fetchone()[0], 11)

    def test_移管は元の利用者の非公開も含めて同組織だけ(self) -> None:
        n = service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(n, 2)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT owner_id FROM notes")}, {3})
