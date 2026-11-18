"""V3: 閲覧の可視性と views。"""
from notes import service
from notes.errors import NotFound
from tests.helpers import BaseCase


class GetTest(BaseCase):
    def test_公開は同組織で見られ_viewsが増加後の値で返る(self) -> None:
        nid = self.make(self.alice, tags=["b", "a"])
        for expected in (1, 2, 3):
            self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], expected)
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(row["views"], 4)
        self.assertEqual(row["tags"], ["a", "b"])
        self.assertEqual(set(row), {"id", "org_id", "owner_id", "title", "body", "private",
                                    "version", "views", "updated_at", "tags"})

    def test_自分の非公開は見られる(self) -> None:
        nid = self.make(self.alice, private=True)
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["views"], 1)

    def test_見えないものは全てNotFoundで同じメッセージ_viewsは増えない(self) -> None:
        pub = self.make(self.alice)
        priv = self.make(self.alice, private=True)
        cases = [("他組織", self.carol, pub), ("他組織の管理者", self.admin2, pub),
                 ("他人の非公開", self.bob, priv), ("同組織管理者でも他人の非公開", self.admin, priv),
                 ("不存在", self.alice, 9999)]
        msgs = set()
        for name, user, nid in cases:
            with self.subTest(name):
                with self.assertRaises(NotFound) as cm:
                    service.get_note(self.conn, user, nid)
                msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(self.conn.execute("SELECT SUM(views) FROM notes").fetchone()[0], 0)

    def test_viewフック中に削除されたらNotFound(self) -> None:
        nid = self.make(self.alice)

        def hook(step: str) -> None:
            if step == "view":
                self.conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
        self.assertFalse(self.conn.in_transaction)
