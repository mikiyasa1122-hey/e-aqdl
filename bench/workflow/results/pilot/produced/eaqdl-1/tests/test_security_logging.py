from notes import service
from notes.errors import Forbidden, NotesError, ValidationError
from tests.helpers import DbTestCase

BODY = "DUMMY-SECRET-BODY-12345"
TITLE_MARK = "DUMMY-TITLE-MARK-777"


class LoggingTest(DbTestCase):
    def test_作成ログにはidだけで本文は出ない(self):
        with self.assertLogs("notes", level="INFO") as cm:
            nid = self.note(owner="bob", title=TITLE_MARK, body=BODY, tags=("t",))
        out = "\n".join(cm.output)
        self.assertEqual(len(cm.records), 1)
        self.assertIn(f"id={nid}", out)
        self.assertIn("owner=2", out)
        for secret in (BODY, TITLE_MARK, self.u["bob"]["api_key"]):
            self.assertNotIn(secret, out)

    def test_全操作を通してログにも例外メッセージにも機密が出ない(self):
        alice, bob, admin = self.u["alice"], self.u["bob"], self.u["admin"]
        secrets = [BODY, TITLE_MARK] + [u["api_key"] for u in self.u.values()]
        messages = []

        def expect(exc_type, fn, *a, **kw):
            with self.assertRaises(exc_type) as cm:
                fn(*a, **kw)
            messages.append(str(cm.exception))

        with self.assertLogs("notes", level="DEBUG") as cm:
            nid = service.create_note(self.conn, alice, TITLE_MARK, BODY, ("t",), True)
            service.get_note(self.conn, alice, nid)
            service.update_note(self.conn, alice, nid, 1, body=BODY + "2")
            expect(ValidationError, service.create_note, self.conn, alice, "  ", BODY)
            expect(ValidationError, service.create_note, self.conn, alice, TITLE_MARK * 20, BODY)
            expect(ValidationError, service.create_note, self.conn, alice, "t", BODY * 1000)
            expect(ValidationError, service.create_note, self.conn, alice, "t", BODY, (BODY,))
            expect(ValidationError, service.search_notes, self.conn, alice, BODY * 10)
            expect(service.NotFound, service.get_note, self.conn, bob, nid)
            expect(Forbidden, service.transfer_notes, self.conn, bob, alice, bob)
            expect(Forbidden, service.export_audit, self.conn, bob)
            expect(service.Conflict, service.update_note, self.conn, alice, nid, 1, body=BODY)
            service.search_notes(self.conn, alice, BODY)
            service.export_audit(self.conn, admin)
            service.delete_note(self.conn, alice, nid)
        text = "\n".join(cm.output + messages) + repr(service.export_audit(self.conn, admin))
        for s in secrets:
            self.assertNotIn(s, text)
        self.assertTrue(messages)
        for m in messages:
            self.assertNotIn("SELECT", m)

    def test_業務例外はNotesErrorで内部の値を含まない(self):
        with self.assertRaises(NotesError) as cm:
            service.create_note(self.conn, self.u["alice"], "t", "b", ("BAD-TAG-VALUE",))
        self.assertNotIn("BAD-TAG-VALUE", str(cm.exception))
