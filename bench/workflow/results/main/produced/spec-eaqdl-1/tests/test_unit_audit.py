"""V6 / V17: R6 監査ログの出力（api_key 伏せ）。"""
from notes import db, service
from notes.errors import Forbidden
from tests import support
from tests.support import BaseCase


class AuditTest(BaseCase):
    def test_unit_audit_末尾4文字以外を伏せる(self):
        service.create_note(self.conn, self.u["alice"], "t", "b")
        out = service.export_audit(self.conn, self.u["admin"])
        self.assertEqual(len(out), 1)
        key = support.KEY_ALICE
        self.assertEqual(out[0]["actor_key"], "*" * (len(key) - 4) + key[-4:])
        self.assertEqual(out[0]["actor"], "alice")
        self.assertEqual((out[0]["action"], out[0]["detail"]), ("create", "note=1"))

    def test_unit_audit_8文字未満は全て伏せ長さを保つ(self):
        cases = {"": "", "a": "*", "abcdefg": "*" * 7, "abcdefgh": "****efgh", "abcdefghi": "*****fghi"}
        for i, (key, masked) in enumerate(cases.items()):
            uid = 100 + i
            user = db.add_user(self.conn, uid, 1, f"u{i}", "member", key)
            service.create_note(self.conn, user, "t", "b")
        out = service.export_audit(self.conn, self.u["admin"])
        self.assertEqual([o["actor_key"] for o in out], list(cases.values()))

    def test_unit_audit_出力全体に生のapi_keyが含まれない(self):
        service.create_note(self.conn, self.u["alice"], "t", "b")
        service.create_note(self.conn, self.u["admin"], "t", "b")
        short = db.add_user(self.conn, 50, 1, "shorty", "member", support.KEY_SHORT)
        service.create_note(self.conn, short, "t", "b")
        text = repr(service.export_audit(self.conn, self.u["admin"]))
        for key in (support.KEY_ALICE, support.KEY_ADMIN, support.KEY_SHORT):
            self.assertNotIn(key, text)
        self.assertNotIn(support.KEY_ALICE[:-4], text)

    def test_unit_audit_管理者以外はForbidden(self):
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.u["alice"])

    def test_unit_audit_他組織は出ずid順(self):
        service.create_note(self.conn, self.u["carol"], "t", "b")
        a = service.create_note(self.conn, self.u["alice"], "t", "b")
        b = service.create_note(self.conn, self.u["bob"], "t", "b")
        out = service.export_audit(self.conn, self.u["admin"])
        self.assertEqual([o["detail"] for o in out], [f"note={a}", f"note={b}"])
        self.assertEqual([o["actor"] for o in out], ["alice", "bob"])
        self.assertEqual([o["id"] for o in out], sorted(o["id"] for o in out))
        out2 = service.export_audit(self.conn, self.u["dora"])
        self.assertEqual([o["actor"] for o in out2], ["carol"])

    def test_unit_audit_利用者が見つからなくても出力できる(self):
        self.conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (1, 999, 'x', 'y')")
        out = service.export_audit(self.conn, self.u["admin"])
        self.assertEqual((out[0]["actor"], out[0]["actor_key"]), (None, None))
