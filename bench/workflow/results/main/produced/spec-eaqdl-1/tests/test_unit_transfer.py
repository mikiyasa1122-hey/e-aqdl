"""V5 / V16: R5 管理者によるメモの移管。"""
from notes import service
from notes.errors import Forbidden, NotFound
from tests.support import BaseCase, count


class TransferTest(BaseCase):
    def make(self, user="alice", **kw):
        return service.create_note(self.conn, self.u[user], "題", "本", **kw)

    def owners(self):
        return [tuple(r) for r in self.conn.execute("SELECT id, owner_id, version FROM notes ORDER BY id")]

    def last_audit(self):
        r = self.conn.execute("SELECT action, detail, actor_id, org_id FROM audit ORDER BY id DESC").fetchone()
        return tuple(r)

    def test_unit_transfer_全て移す_非公開も_versionが増える(self):
        a = self.make()
        b = self.make(private=True)
        c = self.make(user="bob")
        n = service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assertEqual(n, 2)
        self.assertEqual(self.owners(), [(a, 2, 2), (b, 2, 2), (c, 2, 1)])
        self.assertEqual(self.last_audit(), ("transfer", "from=1 to=2 count=2", 3, 1))

    def test_unit_transfer_idでも指定できる(self):
        self.make()
        self.assertEqual(service.transfer_notes(self.conn, self.u["admin"], 1, 2), 1)
        self.assertEqual(self.owners()[0][1], 2)

    def test_unit_transfer_0件でも成功し監査ログを残す(self):
        self.assertEqual(service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"]), 0)
        self.assertEqual(self.last_audit()[:2], ("transfer", "from=1 to=2 count=0"))

    def test_unit_transfer_memberはForbidden(self):
        self.make()
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.u["alice"], self.u["alice"], self.u["bob"])
        self.assertEqual(self.owners(), [(1, 1, 1)])

    def test_unit_transfer_別組織が混ざればForbiddenで何も動かない(self):
        self.make()
        cases = [
            (self.u["admin"], self.u["alice"], self.u["carol"]),  # 先が別組織
            (self.u["admin"], self.u["carol"], self.u["alice"]),  # 元が別組織
            (self.u["dora"], self.u["alice"], self.u["bob"]),     # 管理者が別組織
        ]
        for admin, f, t in cases:
            with self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, admin, f, t)
        self.assertEqual(self.owners(), [(1, 1, 1)])
        self.assertEqual(count(self.conn, "audit"), 1)

    def test_unit_transfer_dictのorg_idを偽ってもDBの組織で判定する(self):
        self.make()
        fake = dict(self.u["carol"], org_id=1)  # 組織を偽った dict
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], fake)
        self.assertEqual(self.owners(), [(1, 1, 1)])

    def test_unit_transfer_存在しない利用者はNotFound(self):
        self.make()
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u["admin"], 999, self.u["bob"])
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], 999)
        self.assertEqual(self.owners(), [(1, 1, 1)])

    def test_unit_transfer_途中の失敗で全て元のまま(self):
        a = self.make()
        b = self.make()
        c = self.make()
        self.fail_at(f"transfer:{b}")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assertEqual(self.owners(), [(a, 1, 1), (b, 1, 1), (c, 1, 1)])
        self.assertEqual(count(self.conn, "audit"), 3)  # 作成分だけ。移管の監査ログなし
        self.assertFalse(self.conn.in_transaction)
