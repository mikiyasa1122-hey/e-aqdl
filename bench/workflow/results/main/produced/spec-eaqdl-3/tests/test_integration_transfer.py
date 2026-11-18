"""V5 V17: 管理者による全件移管。"""
from notes import db, service
from notes.errors import Forbidden, NotFound
from tests.helpers import DbTestCase


class Transfer(DbTestCase):
    def setUp(self):
        super().setUp()
        self.n1 = service.create_note(self.conn, self.alice, "1", "b")
        self.n2 = service.create_note(self.conn, self.alice, "2", "b", private=True)
        self.n3 = service.create_note(self.conn, self.alice, "3", "b")
        self.bobs = service.create_note(self.conn, self.bob, "bob", "b")

    def state(self):
        return [(r["id"], r["owner_id"], r["version"]) for r in
                self.conn.execute("SELECT id, owner_id, version FROM notes ORDER BY id")]

    def test_全件移管(self):
        n = service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(n, 3)
        b = self.bob["id"]
        self.assertEqual(self.state(), [(self.n1, b, 2), (self.n2, b, 2), (self.n3, b, 2),
                                        (self.bobs, b, 1)])
        last = service.export_audit(self.conn, self.admin)[-1]
        self.assertEqual((last["action"], last["detail"]),
                         ("transfer", f"from={self.alice['id']} to={b} count=3"))
        self.assertEqual(last["actor"], "admin")

    def test_ゼロ件でも成功し記録される(self):
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.admin2, self.bob), 0)
        self.assertIn("count=0", service.export_audit(self.conn, self.admin)[-1]["detail"])

    def test_権限と組織(self):
        before, audit = self.state(), self.count("audit")
        cases = [
            (self.alice, self.alice, self.bob),        # member は不可
            (self.bob, self.alice, self.bob),
            (self.eve_admin, self.alice, self.bob),    # 他組織の管理者
            (self.admin, self.alice, self.eve),        # 先が他組織
            (self.admin, self.eve, self.bob),          # 元が他組織
            (self.eve_admin, self.alice, self.eve),
        ]
        for admin, src, dst in cases:
            with self.subTest(admin=admin["name"], src=src["name"], dst=dst["name"]), \
                    self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, admin, src, dst)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.count("audit"), audit)

    def test_dictの組織を偽装しても越境できない(self):
        fake_eve = dict(self.eve, org_id=1)  # 呼び出し側が org_id を偽っても DB の値で判定
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.admin, self.alice, fake_eve)

    def test_存在しない利用者はNotFound(self):
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.admin, self.alice, {"id": 9999})

    def test_途中失敗で全件元のまま(self):
        before, audit = self.state(), self.count("audit")

        def hook(step):
            if step == f"transfer:{self.n2}":
                raise RuntimeError("注入した失敗")
        self.set_hook(hook)
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.state(), before)  # 1件目も戻る
        self.assertEqual(self.count("audit"), audit)
        self.assertFalse(self.conn.in_transaction)
