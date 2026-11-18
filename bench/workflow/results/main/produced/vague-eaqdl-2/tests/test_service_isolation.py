"""AC1 AC2: 組織分離と非公開（全関数）。org 条件・private 条件を外すと失敗する。"""
from notes import service
from notes.errors import Forbidden, NotFound
from tests.helpers import MemCase, snapshot


class IsolationUnit(MemCase):
    def setUp(self):
        super().setUp()
        self.pub = self.note(self.alice, "KEYWORD公開", "KEYWORD", ["t"])
        self.priv = self.note(self.alice, "KEYWORD非公開", "KEYWORD", ["t"], private=True)

    def _ops(self, user, nid):
        return {
            "get": lambda: service.get_note(self.conn, user, nid),
            "update": lambda: service.update_note(self.conn, user, nid, 1, title="x"),
            "delete": lambda: service.delete_note(self.conn, user, nid),
        }

    def test_他組織は全操作でNotFound_管理者でも(self):  # V1 V21
        for user_name in ("carol", "root2"):
            for nid in (self.pub, self.priv):
                for op, fn in self._ops(self.u[user_name], nid).items():
                    with self.subTest(user=user_name, nid=nid, op=op):
                        before = snapshot(self.conn)
                        with self.assertRaises(NotFound):
                            fn()
                        self.assertEqual(snapshot(self.conn), before)

    def test_他組織のlist_searchに出ない(self):  # V1 V21
        for user_name in ("carol", "root2"):
            u = self.u[user_name]
            with self.subTest(user=user_name):
                self.assertEqual(service.list_notes(self.conn, u), {
                    "items": [], "total": 0, "page": 1, "per_page": 20})
                self.assertEqual(service.search_notes(self.conn, u, "KEYWORD"), [])
                self.assertEqual(service.search_notes(self.conn, u, "KEYWORD", ["t"]), [])

    def test_他組織のtransferはNotFound(self):  # V1 V21
        before = snapshot(self.conn)
        # 組織2の管理者が組織1の利用者のメモを引き継ごうとする
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.root2, 1, 4)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.root2, 4, 1)
        # 組織1の管理者が組織2の利用者を宛先にする
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.root1, 1, 4)
        self.assertEqual(snapshot(self.conn), before)

    def test_存在有無が区別できない(self):  # V1 V21
        """他組織のメモと存在しないメモで、例外の型もメッセージも同じ。"""
        msgs = set()
        for nid in (self.pub, 99999):
            for fn in (lambda n=nid: service.get_note(self.conn, self.carol, n),
                       lambda n=nid: service.update_note(self.conn, self.carol, n, 1),
                       lambda n=nid: service.delete_note(self.conn, self.carol, n)):
                with self.assertRaises(NotFound) as cm:
                    fn()
                msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)

    def test_非公開は同組織の他者に見えない_管理者を含む(self):  # V2 V22
        for user_name in ("bob", "root1"):
            u = self.u[user_name]
            with self.subTest(user=user_name):
                for op, fn in self._ops(u, self.priv).items():
                    with self.subTest(op=op), self.assertRaises(NotFound):
                        fn()
                res = service.list_notes(self.conn, u)
                self.assertEqual([i["id"] for i in res["items"]], [self.pub])
                self.assertEqual(res["total"], 1)
                self.assertEqual([r["id"] for r in service.search_notes(self.conn, u, "KEYWORD")],
                                 [self.pub])
                self.assertEqual(service.search_notes(self.conn, u, "KEYWORD非公開"), [])

    def test_非公開は作成者本人には見える(self):  # V2 V22
        self.assertEqual(service.get_note(self.conn, self.alice, self.priv)["id"], self.priv)
        self.assertEqual(service.list_notes(self.conn, self.alice)["total"], 2)
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "KEYWORD")), 2)
        self.assertEqual(service.update_note(self.conn, self.alice, self.priv, 1, title="n"), 2)
        service.delete_note(self.conn, self.alice, self.priv)

    def test_他人の非公開は閲覧回数も変えない(self):  # V2 V22
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.root1, self.priv)
        views = self.conn.execute("SELECT views FROM notes WHERE id = ?", (self.priv,)).fetchone()[0]
        self.assertEqual(views, 0)

    def test_公開メモの他メンバーはgetできるが更新はForbidden(self):  # V22
        self.assertEqual(service.get_note(self.conn, self.bob, self.pub)["id"], self.pub)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, self.pub, 1)
