import sqlite3

from notes import service
from notes.errors import Forbidden
from tests.helpers import DbTestCase, snapshot


class TransferTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.a1 = self.note(owner="alice")
        self.a2 = self.note(owner="alice", private=True, tags=("t",))
        self.a3 = self.note(owner="alice")
        self.b1 = self.note(owner="bob")
        self.c1 = self.note(owner="carol")

    def owners(self):
        return {r["id"]: (r["owner_id"], r["version"]) for r in self.conn.execute("SELECT * FROM notes")}

    def test_transfer_全件移管しversionを1増やす(self):
        n = service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assertEqual(n, 3)
        o = self.owners()
        for nid in (self.a1, self.a2, self.a3):
            self.assertEqual(o[nid], (2, 2))  # 非公開も含めて移る
        self.assertEqual(o[self.b1], (2, 1))
        self.assertEqual(o[self.c1], (4, 1))
        det = self.conn.execute("SELECT detail FROM audit WHERE action='transfer'").fetchone()[0]
        self.assertEqual(det, "from=1 to=2 count=3")

    def test_transfer_該当なしは0件で監査ログだけ(self):
        n = service.transfer_notes(self.conn, self.u["admin"], self.u["bob"], self.u["alice"])
        self.assertEqual(n, 1)
        n = service.transfer_notes(self.conn, self.u["admin"], self.u["admin"], self.u["alice"])
        self.assertEqual(n, 0)
        self.assertEqual(self.conn.execute("SELECT detail FROM audit WHERE detail LIKE '%count=0'")
                         .fetchone()[0], "from=3 to=1 count=0")

    def test_transfer_管理者でなければForbidden(self):
        before = snapshot(self.conn)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.u["alice"], self.u["alice"], self.u["bob"])
        self.assertEqual(snapshot(self.conn), before)

    def test_transfer_他組織が絡むとForbiddenで件数もversionも不変(self):
        before = snapshot(self.conn)
        cases = [
            (self.u["admin"], self.u["alice"], self.u["carol"]),   # 移管先が他組織
            (self.u["admin"], self.u["carol"], self.u["alice"]),   # 移管元が他組織
            (self.u["root2"], self.u["alice"], self.u["bob"]),     # 管理者が他組織
            (self.u["root2"], self.u["carol"], self.u["alice"]),
        ]
        for admin, f, t in cases:
            with self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, admin, f, t)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.owners()[self.c1], (4, 1))

    def test_transfer_途中失敗で全体がロールバック(self):
        before = snapshot(self.conn)
        self.fail_at(f"transfer:{self.a2}", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.owners()[self.a1], (1, 1))  # 1件目も元のまま
        self.assertFalse(self.conn.in_transaction)

    def test_transfer_フックは各メモの直前にid順で呼ばれる(self):
        seen = []
        self.set_hook(seen.append)
        service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], self.u["bob"])
        self.assertEqual(seen, [f"transfer:{i}" for i in (self.a1, self.a2, self.a3)])

    def test_transfer_存在しない移管先は全体ロールバック(self):
        before = snapshot(self.conn)
        ghost = {"id": 999, "org_id": 1, "name": "ghost", "role": "member", "api_key": "k"}
        with self.assertRaises(sqlite3.IntegrityError):
            service.transfer_notes(self.conn, self.u["admin"], self.u["alice"], ghost)
        self.assertEqual(snapshot(self.conn), before)
        self.assertFalse(self.conn.in_transaction)
