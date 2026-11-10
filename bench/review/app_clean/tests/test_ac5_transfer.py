"""AC5・K6: R5 の規則どおりに移管・拒否され、失敗したら元のまま。"""
from notes import service
from notes.errors import Forbidden

from .base import Base


class TransferTest(Base):
    def setUp(self):
        super().setUp()
        self.ids = [service.create_note(self.conn, self.alice, f"t{i}", "b", private=(i == 0)) for i in range(3)]
        service.create_note(self.conn, self.bob, "bobs", "b")

    def owners(self):
        return [r[0] for r in self.conn.execute("SELECT owner_id FROM notes ORDER BY id")]

    def test_moves_all_notes_of_the_user(self):
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 3)
        self.assertEqual(self.owners(), [2, 2, 2, 2])
        self.assertEqual(service.get_note(self.conn, self.bob, self.ids[0])["title"], "t0")  # 非公開も移る
        self.assertEqual(self.conn.execute("SELECT action, detail FROM audit ORDER BY id DESC").fetchone()[:],
                         ("transfer", "from=1 to=2 count=3"))

    def test_only_admin(self):
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.bob, self.alice, self.bob)
        self.assertEqual(self.owners(), [1, 1, 1, 2])

    def test_only_within_one_org(self):
        for admin, src, dst in ((self.admin, self.alice, self.eve), (self.admin, self.eve, self.alice),
                                (self.eve_admin, self.alice, self.bob), (self.eve_admin, self.eve, self.alice)):
            with self.subTest(admin=admin["name"], src=src["name"], dst=dst["name"]), self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, admin, src, dst)
        self.assertEqual(self.owners(), [1, 1, 1, 2])

    def test_failure_in_the_middle_moves_nothing(self):
        self.fail_at(f"transfer:{self.ids[1]}")
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.owners(), [1, 1, 1, 2])
        self.assertFalse(self.conn.in_transaction)

    def test_transfer_bumps_version(self):
        service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT version FROM notes ORDER BY id")], [2, 2, 2, 1])
