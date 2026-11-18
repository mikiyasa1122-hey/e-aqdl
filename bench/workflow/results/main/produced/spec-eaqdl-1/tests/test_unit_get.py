"""V2 / V12: R2 閲覧（可視条件・閲覧数）。"""
from notes import db, service
from notes.errors import NotFound
from tests.support import BaseCase


class GetTest(BaseCase):
    def make(self, user="alice", **kw):
        return service.create_note(self.conn, self.u[user], kw.pop("title", "題"), kw.pop("body", "本"), **kw)

    def test_unit_get_全ての列を返し_viewsは増加後の値(self):
        nid = self.make(tags=["b", "a"])
        n1 = service.get_note(self.conn, self.u["bob"], nid)
        self.assertEqual(n1["views"], 1)
        self.assertEqual(n1["tags"], ["a", "b"])
        for col in ("id", "org_id", "owner_id", "title", "body", "private", "version", "views", "updated_at"):
            self.assertIn(col, n1)
        self.assertEqual((n1["title"], n1["body"], n1["owner_id"], n1["org_id"]), ("題", "本", 1, 1))
        self.assertEqual(service.get_note(self.conn, self.u["alice"], nid)["views"], 2)
        self.assertEqual(service.get_note(self.conn, self.u["alice"], nid)["views"], 3)

    def test_unit_get_見えないものは全てNotFoundで区別できない(self):
        pub = self.make()
        priv = self.make(private=True)
        cases = [
            (self.u["carol"], pub),   # 他組織
            (self.u["bob"], priv),    # 他人の非公開
            (self.u["admin"], priv),  # 管理者でも他人の非公開
            (self.u["dora"], priv),   # 他組織の管理者
            (self.u["alice"], 9999),  # 存在しない
        ]
        msgs = set()
        for user, nid in cases:
            with self.assertRaises(NotFound) as cm:
                service.get_note(self.conn, user, nid)
            msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)
        # 失敗では views が増えない
        self.assertEqual(self.conn.execute("SELECT views FROM notes WHERE id=?", (pub,)).fetchone()[0], 0)
        # 作成者本人は非公開を見られる
        self.assertEqual(service.get_note(self.conn, self.u["alice"], priv)["views"], 1)

    def test_unit_get_割り込みの閲覧でも数え漏れない(self):
        nid = self.make()
        other_calls = []
        entered = []

        def hook(step):
            if step == "view" and not entered:
                entered.append(True)
                other_calls.append(service.get_note(self.conn, self.u["bob"], nid)["views"])

        self.set_hook(hook)
        n = service.get_note(self.conn, self.u["alice"], nid)
        self.assertEqual(other_calls, [1])
        self.assertEqual(n["views"], 2)
        self.assertEqual(self.conn.execute("SELECT views FROM notes WHERE id=?", (nid,)).fetchone()[0], 2)

    def test_unit_get_割り込みで削除されたらNotFound(self):
        nid = self.make()

        def hook(step):
            if step == "view":
                self.conn.execute("DELETE FROM notes WHERE id = ?", (nid,))

        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.u["alice"], nid)

    def test_unit_get_割り込みで非公開化されたら他人はNotFound(self):
        nid = self.make()

        def hook(step):
            if step == "view":
                self.conn.execute("UPDATE notes SET private = 1 WHERE id = ?", (nid,))

        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.u["bob"], nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)

    def test_unit_get_dictのorg_idを偽っても他組織は見えない(self):
        nid = self.make()
        carol = db.get_user(self.conn, 4)
        # 認可は渡された dict の org_id で判定する（信用する仕様）が、DB の行の org_id と一致しなければ見えない
        with self.assertRaises(NotFound):
            service.get_note(self.conn, carol, nid)
