"""AC13: 監査ログの出力と伏せ字。"""
from notes import db, service
from notes.errors import Forbidden
from tests.helpers import MemCase


class AuditUnit(MemCase):
    def test_伏せ字(self):  # V13 V28
        cases = [("ABCDEFGH1234", "********1234"), ("12345", "*2345"), ("1234", "****"),
                 ("abc", "***"), ("a", "*"), ("", "")]
        for key, expect in cases:
            self.assertEqual(service._mask(key), expect, key)
        self.assertEqual(service._mask(None), "")

    def test_adminのみで自組織の分だけid順(self):  # V13 V28
        a = self.note(self.alice)
        service.update_note(self.conn, self.alice, a, 1, title="x")
        self.note(self.bob)
        self.note(self.carol)  # 他組織
        res = service.export_audit(self.conn, self.root1)
        self.assertEqual([r["id"] for r in res], sorted(r["id"] for r in res))
        self.assertEqual([(r["actor"], r["action"]) for r in res],
                         [("alice", "create"), ("alice", "update"), ("bob", "create")])
        self.assertEqual(res[0]["detail"], f"note={a}")
        self.assertEqual(set(res[0]), {"id", "action", "detail", "actor", "actor_key"})
        other = service.export_audit(self.conn, self.root2)
        self.assertEqual([r["actor"] for r in other], ["carol"])

    def test_鍵の全文が結果に無い(self):  # V13 V28
        self.note(self.alice)
        res = service.export_audit(self.conn, self.root1)
        self.assertEqual(res[0]["actor_key"], "*" * (len("test-key-alice-0001") - 4) + "0001")
        self.assertNotIn("test-key-alice-0001", repr(res))

    def test_短い鍵は全伏せ(self):  # V13
        db.add_user(self.conn, 10, 1, "shorty", "member", "abcd")
        db.add_user(self.conn, 11, 1, "tiny", "member", "xyz")
        for uid, expect in ((10, "****"), (11, "***")):
            user = db.get_user(self.conn, uid)
            service.create_note(self.conn, user, "t", "b")
        keys = [r["actor_key"] for r in service.export_audit(self.conn, self.root1)]
        self.assertEqual(keys, ["****", "***"])

    def test_admin以外はForbidden(self):  # V13 V28
        self.note(self.alice)
        for user in (self.alice, self.bob, self.carol):
            with self.subTest(user=user["name"]), self.assertRaises(Forbidden):
                service.export_audit(self.conn, user)

    def test_ログが空でも空リスト(self):  # V13
        self.assertEqual(service.export_audit(self.conn, self.root1), [])
