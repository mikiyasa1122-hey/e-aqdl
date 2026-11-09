"""AC9・K5: ログ・例外のメッセージ・出力に、本文・署名・api_key を出さない。"""
import logging

from notes import service, share
from notes.errors import NotesError

from .base import Base

BODY = "TOP-SECRET-BODY-TEXT"


class SecretsTest(Base):
    def capture(self):
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        h = H(level=logging.DEBUG)
        for name in ("notes", "notes.share"):
            lg = logging.getLogger(name)
            lg.addHandler(h)
            lg.setLevel(logging.DEBUG)
            self.addCleanup(lg.removeHandler, h)
        return records

    def test_logs_have_no_body_signature_or_key(self):
        records = self.capture()
        nid = service.create_note(self.conn, self.alice, "t", BODY, ["a"])
        service.update_note(self.conn, self.alice, nid, 1, body=BODY + "2")
        link = share.make_link("s3cret", nid, now=1000)
        sig = link.split(".")[2]
        text = "\n".join(records)
        self.assertTrue(records)
        for secret in (BODY, sig, "ak-alice-0001", "s3cret"):
            self.assertNotIn(secret, text)

    def test_exception_messages_have_no_body_or_key(self):
        nid = service.create_note(self.conn, self.alice, "t", BODY, private=True)
        msgs = []
        calls = [lambda: service.get_note(self.conn, self.bob, nid),
                 lambda: service.update_note(self.conn, self.alice, nid, 9, body=BODY),
                 lambda: service.update_note(self.conn, self.alice, nid, 1, body=BODY * 1000),
                 lambda: service.delete_note(self.conn, self.eve, nid),
                 lambda: share.verify_link("s3cret", share.make_link("s3cret", nid, now=1000)[:-1], now=1001)]
        for call in calls:
            try:
                call()
            except NotesError as e:
                msgs.append(str(e))
        self.assertEqual(len(msgs), len(calls))
        for m in msgs:
            self.assertNotIn(BODY, m)
            self.assertNotIn("ak-", m)

    def test_audit_detail_has_no_body(self):
        service.create_note(self.conn, self.alice, "t", BODY)
        for r in service.export_audit(self.conn, self.admin):
            self.assertNotIn(BODY, str(r))
