"""V13, V14: 移管。"""
from notes import db, service
from notes.errors import Forbidden, ValidationError
from tests.helpers import BaseCase


class TransferTest(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.n1 = self.make(self.alice, title="n1")
        self.n2 = self.make(self.alice, title="n2", private=True)
        self.n3 = self.make(self.bob, title="n3")
        service.update_note(self.conn, self.alice, self.n1, 1, title="n1b")  # version=2

    def test_全件移管_件数_version_監査(self) -> None:
        n = service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(n, 2)
        rows = {r["id"]: r for r in self.conn.execute("SELECT * FROM notes")}
        self.assertEqual((rows[self.n1]["owner_id"], rows[self.n1]["version"]), (3, 3))
        self.assertEqual((rows[self.n2]["owner_id"], rows[self.n2]["version"]), (3, 2))
        self.assertEqual((rows[self.n3]["owner_id"], rows[self.n3]["version"]), (3, 1))
        r = self.conn.execute("SELECT org_id, actor_id, action, detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(r), (1, 1, "transfer", "from=2 to=3 count=2"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'transfer'").fetchone()[0], 1)

    def test_0件でも成功し監査を残す(self) -> None:
        self.assertEqual(service.transfer_notes(self.conn, self.admin, db.add_user(self.conn, 4, 1, "dave"), self.bob), 0)

    def test_管理者以外はForbidden(self) -> None:
        for actor in (self.alice, self.bob):
            with self.subTest(actor["name"]), self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, actor, self.alice, self.bob)

    def test_組織違いはForbidden_全件不変(self) -> None:
        before = self.snapshot()
        cases = [("元が別組織", self.admin, self.carol, self.bob), ("先が別組織", self.admin, self.alice, self.carol),
                 ("管理者が別組織", self.admin2, self.alice, self.bob)]
        for name, a, f, t in cases:
            with self.subTest(name), self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, a, f, t)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'transfer'").fetchone()[0], 0)

    def test_roleを偽っても利用者dictのroleで判定(self) -> None:
        # member の dict は admin 扱いにならない
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, dict(self.alice), self.alice, self.bob)

    def test_途中失敗で全件元のまま(self) -> None:
        before = self.snapshot()
        audit_before = self.counts()["audit"]
        self.fail_at(f"transfer:{self.n2}")  # 1件目は更新済みの状態で2件目が失敗
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.counts()["audit"], audit_before)
        self.assertFalse(self.conn.in_transaction)

    def test_最初のメモで失敗しても全件元のまま(self) -> None:
        before = self.snapshot()
        self.fail_at(f"transfer:{self.n1}")
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.snapshot(), before)

    def test_元と先が同じはValidationError(self) -> None:
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.alice)
