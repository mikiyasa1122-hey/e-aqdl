"""V14 V15: 監査ログの出力。"""
from notes import db, service
from notes.errors import Forbidden
from tests.helpers import NotesTestCase, fake_key


class AuditTest(NotesTestCase):
    def test_audit_権限と各操作の記録(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.update_note(self.conn, self.alice, nid, 1, title="u")
        service.transfer_notes(self.conn, self.carol, self.alice, self.bob)
        service.delete_note(self.conn, self.bob, nid)
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.bob)
        self.assertEqual(service.export_audit(self.conn, self.erin), [])  # 他組織の管理者には自組織分だけ
        out = service.export_audit(self.conn, self.carol)
        self.assertEqual([(o["action"], o["detail"], o["actor"]) for o in out], [
            ("create", f"note={nid}", "alice"),
            ("update", f"note={nid}", "alice"),
            ("transfer", "from=1 to=2 count=1", "carol"),
            ("delete", f"note={nid}", "bob"),
        ])
        self.assertEqual([o["id"] for o in out], sorted(o["id"] for o in out))
        self.assertEqual(set(out[0]), {"id", "action", "detail", "actor", "actor_key"})

    def test_audit_自組織の記録だけ(self) -> None:
        service.create_note(self.conn, self.alice, "t", "b")
        service.create_note(self.conn, self.dave, "t", "b")
        self.assertEqual([o["actor"] for o in service.export_audit(self.conn, self.carol)], ["alice"])
        self.assertEqual([o["actor"] for o in service.export_audit(self.conn, self.erin)], ["dave"])

    def test_audit_api_key全体がどこにも出ない(self) -> None:
        for uid, key in ((6, "ab"), (7, "abcd"), (8, "abcde"), (9, fake_key("long-value-here"))):
            db.add_user(self.conn, uid, 1, f"u{uid}", "member", key)
        users = [self.alice, self.carol, *(db.get_user(self.conn, i) for i in (6, 7, 8, 9))]
        for u in users:
            service.create_note(self.conn, u, "t", "b")
        out = service.export_audit(self.conn, self.carol)
        keys = {o["actor"]: o["actor_key"] for o in out}
        self.assertEqual(keys["alice"], "****" + fake_key("alice")[-4:])
        self.assertEqual(keys["u6"], "****")
        self.assertEqual(keys["u7"], "****")
        self.assertEqual(keys["u8"], "****bcde")
        for o in out:
            for value in o.values():
                for key in (self.alice["api_key"], self.carol["api_key"], fake_key("long-value-here")):
                    self.assertNotIn(key, str(value))
        for key in ("ab", "abcd", fake_key("long-value-here"), self.alice["api_key"]):
            self.assertNotIn(key, repr(out))

    def test_audit_他組織の鍵は混ざらない(self) -> None:
        service.create_note(self.conn, self.dave, "t", "b")
        service.create_note(self.conn, self.alice, "t", "b")
        self.assertNotIn(self.dave["api_key"][-4:] + "x", repr(service.export_audit(self.conn, self.carol)))
        self.assertNotIn("dave", repr(service.export_audit(self.conn, self.carol)))
