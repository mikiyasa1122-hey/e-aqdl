"""AC6・K7: 監査ログの api_key が伏せられている（管理者だけ）。"""
from notes import db, service
from notes.errors import Forbidden

from .base import Base


class AuditTest(Base):
    def test_only_admin_and_own_org(self):
        service.create_note(self.conn, self.alice, "a", "b")
        service.create_note(self.conn, self.eve, "a", "b")
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)
        rows = service.export_audit(self.conn, self.admin)
        self.assertEqual([(r["action"], r["actor"]) for r in rows], [("create", "alice")])

    def test_key_masked_except_last_four(self):
        service.create_note(self.conn, self.alice, "a", "b")
        key = service.export_audit(self.conn, self.admin)[0]["actor_key"]
        self.assertEqual(key, "*" * 9 + "0001")
        self.assertNotIn("ak-alice", key)

    def test_short_keys_fully_masked(self):
        for i, key in enumerate(("1234567", "12345678", "")):
            u = db.add_user(self.conn, 100 + i, 10, f"u{i}", api_key=key)
            service.create_note(self.conn, u, "a", "b")
        got = [r["actor_key"] for r in service.export_audit(self.conn, self.admin)]
        self.assertEqual(got, ["*******", "****5678", ""])
