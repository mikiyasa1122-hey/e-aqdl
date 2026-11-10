from notes import db, service
from notes.errors import Forbidden
from tests.helpers import DbTestCase


class AuditTest(DbTestCase):
    def export(self, who="admin"):
        return service.export_audit(self.conn, self.u[who])

    def test_audit_8文字以上は末尾4文字以外を伏せる(self):
        db.add_user(self.conn, 10, 1, "k8", "member", "abcdefgh")
        db.add_user(self.conn, 11, 1, "k12", "member", "abcdefghijkl")
        for uid in (10, 11):
            service.create_note(self.conn, db.get_user(self.conn, uid), "t", "b")
        res = {r["actor"]: r["actor_key"] for r in self.export()}
        self.assertEqual(res["k8"], "****efgh")
        self.assertEqual(res["k12"], "********ijkl")

    def test_audit_8文字未満は全て伏せる(self):
        for uid, key in ((10, "abcdefg"), (11, "abc"), (12, "a"), (13, "")):
            db.add_user(self.conn, uid, 1, f"k{uid}", "member", key)
            service.create_note(self.conn, db.get_user(self.conn, uid), "t", "b")
        res = {r["actor"]: r["actor_key"] for r in self.export()}
        self.assertEqual(res["k10"], "*" * 7)
        self.assertEqual(res["k11"], "***")
        self.assertEqual(res["k12"], "*")
        self.assertEqual(res["k13"], "")

    def test_audit_生のapi_keyが結果のどこにも出ない(self):
        self.note(owner="alice")
        self.note(owner="bob")
        text = repr(self.export())
        for u in self.u.values():
            self.assertNotIn(u["api_key"], text)
            self.assertNotIn(u["api_key"][:-4], text)

    def test_audit_管理者だけ_自組織の行だけid順(self):
        a = self.note(owner="alice")
        self.note(owner="carol")
        self.note(owner="bob")
        res = self.export()
        self.assertEqual([r["id"] for r in res], [1, 3])
        self.assertEqual(res[0], {"id": 1, "action": "create", "detail": f"note={a}",
                                  "actor": "alice", "actor_key": "*" * 16 + "0001"})
        for who in ("alice", "carol"):
            with self.assertRaises(Forbidden):
                self.export(who)

    def test_audit_操作者が存在しない行は空文字(self):
        self.conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (1, 999, 'x', 'y')")
        res = self.export()
        self.assertEqual((res[0]["actor"], res[0]["actor_key"]), ("", ""))
