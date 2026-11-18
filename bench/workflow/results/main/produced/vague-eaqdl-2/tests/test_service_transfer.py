"""AC11 AC12: 一括引き継ぎ。"""
from unittest import mock

from notes import service
from notes.errors import Forbidden, NotFound, ValidationError
from tests.helpers import ALICE, BOB, CAROL, ROOT1, MemCase, hook, snapshot


class TransferUnit(MemCase):
    def setUp(self):
        super().setUp()
        self.n1 = self.note(self.alice, "a1")
        self.n2 = self.note(self.alice, "a2", private=True)
        self.n3 = self.note(self.alice, "a3")
        self.other = self.note(self.bob, "b1")
        service.update_note(self.conn, self.alice, self.n1, 1, title="a1v2")  # n1 は version 2

    def _row(self, nid):
        return dict(self.conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone())

    def test_全件引き継ぎ_件数_version_private_監査ログ(self):  # V11
        with mock.patch.object(service, "_now", return_value=4000000000):
            n = service.transfer_notes(self.conn, self.root1, ALICE, BOB)
        self.assertEqual(n, 3)
        self.assertEqual([self._row(i)["owner_id"] for i in (self.n1, self.n2, self.n3)], [BOB] * 3)
        self.assertEqual([self._row(i)["version"] for i in (self.n1, self.n2, self.n3)], [3, 2, 2])
        self.assertEqual(self._row(self.n2)["private"], 1)
        self.assertEqual(self._row(self.n1)["updated_at"], 4000000000)
        self.assertEqual(self._row(self.other)["version"], 1)  # 無関係のメモは変わらない
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT org_id, actor_id, detail FROM audit WHERE action = 'transfer'")]
        self.assertEqual(rows, [(1, ROOT1, f"from={ALICE} to={BOB} count=3")])
        # 非公開は移した先の所有者に見え、元の所有者には見えない
        self.assertEqual(service.get_note(self.conn, self.bob, self.n2)["id"], self.n2)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, self.n2)

    def test_0件でも監査ログを1件残す(self):  # V11
        self.assertEqual(service.transfer_notes(self.conn, self.root1, ROOT1, BOB), 0)
        detail = self.conn.execute("SELECT detail FROM audit WHERE action='transfer'").fetchone()[0]
        self.assertEqual(detail, f"from={ROOT1} to={BOB} count=0")

    def test_admin以外はForbidden(self):  # V12 V27
        before = snapshot(self.conn)
        for user in (self.alice, self.bob):
            with self.subTest(user=user["name"]), self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, user, ALICE, BOB)
        self.assertEqual(snapshot(self.conn), before)

    def test_不正な宛先は何も変えない(self):  # V12 V27
        cases = [
            ("同一人物", ALICE, ALICE, ValidationError),
            ("他組織の宛先", ALICE, CAROL, NotFound),
            ("他組織の元", CAROL, ALICE, NotFound),
            ("存在しない宛先", ALICE, 999, NotFound),
            ("存在しない元", 999, BOB, NotFound),
            ("範囲外", ALICE, 2 ** 70, NotFound),
            ("型違い", "1", BOB, ValidationError),
            ("bool", True, BOB, ValidationError),
        ]
        before = snapshot(self.conn)
        for name, f, t, exc in cases:
            with self.subTest(name), self.assertRaises(exc):
                service.transfer_notes(self.conn, self.root1, f, t)
        self.assertEqual(snapshot(self.conn), before)

    def test_途中で失敗したら全件が元のまま(self):  # V12 V27
        before = snapshot(self.conn)
        calls = []

        def boom(step):
            calls.append(step)
            if len(calls) == 2:
                raise RuntimeError("途中失敗")
        with hook(boom), self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.root1, ALICE, BOB)
        self.assertEqual(calls, [f"transfer:{self.n1}", f"transfer:{self.n2}"])
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit WHERE action='transfer'").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)
