"""V9 / V15: R9 ログ・例外・出力に機密情報を出さない。"""
import logging

from notes import service, share
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests import support
from tests.support import BaseCase

BODY = "本文SECRETBODY-zzz"
TITLE = "題名SECRETTITLE-yyy"
SECRET = "dummy-secret-for-test"


class SecurityLoggingTest(BaseCase):
    def test_unit_log_作成ログにidはあるが本文題名api_keyはない(self):
        with self.assertLogs("notes", level="INFO") as cm:
            nid = service.create_note(self.conn, self.u["alice"], TITLE, BODY, tags=["tagsecret"])
        text = "\n".join(cm.output)
        self.assertIn(f"id={nid}", text)
        self.assertIn("owner=1", text)
        for s in (BODY, TITLE, "tagsecret", support.KEY_ALICE):
            self.assertNotIn(s, text)

    def test_unit_log_失敗した作成は作成ログを出さない(self):
        self.fail_at("create:tags")
        with self.assertLogs("notes", level="INFO") as cm:
            logging.getLogger("notes").info("sentinel")
            try:
                service.create_note(self.conn, self.u["alice"], TITLE, BODY)
            except RuntimeError:
                pass
        self.assertEqual(len(cm.output), 1)

    def test_unit_exc_例外メッセージに本文題名鍵が出ない(self):
        nid = service.create_note(self.conn, self.u["alice"], TITLE, BODY, private=True)
        pub = service.create_note(self.conn, self.u["alice"], TITLE, BODY)
        secrets = (BODY, TITLE, support.KEY_ALICE, support.KEY_BOB, "tagsecret")
        errors = []

        def capture(exc_type, fn):
            with self.assertRaises(exc_type) as cm:
                fn()
            errors.append(cm.exception)

        capture(NotFound, lambda: service.get_note(self.conn, self.u["bob"], nid))
        capture(Forbidden, lambda: service.update_note(self.conn, self.u["bob"], pub, 1, title="x"))
        capture(Conflict, lambda: service.update_note(self.conn, self.u["alice"], pub, 9, title="x"))
        capture(ValidationError, lambda: service.create_note(self.conn, self.u["alice"], TITLE * 10, BODY))
        capture(ValidationError, lambda: service.create_note(self.conn, self.u["alice"], "t", BODY * 1000))
        capture(ValidationError, lambda: service.create_note(self.conn, self.u["alice"], "t", "b", ["tagsecret!"]))
        capture(ValidationError, lambda: service.search_notes(self.conn, self.u["alice"], "x" * 200 + TITLE))
        capture(Forbidden, lambda: service.export_audit(self.conn, self.u["bob"]))
        capture(Forbidden, lambda: service.transfer_notes(self.conn, self.u["bob"], 1, 2))
        link = share.make_link(SECRET, 1, now=0)
        sig = link.split(".")[2]
        capture(Forbidden, lambda: share.verify_link(SECRET, link, now=10**9))
        capture(Forbidden, lambda: share.verify_link(SECRET, link[:-1] + "x", now=0))
        for e in errors:
            for s in secrets + (sig, link):
                self.assertNotIn(s, str(e))
                self.assertNotIn(s, repr(e))

    def test_unit_log_リンクの成功失敗どちらでも署名がログに出ない(self):
        link = share.make_link(SECRET, 5, now=0)
        sig = link.split(".")[2]
        flipped = link[:-1] + ("0" if link[-1] != "0" else "1")
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            logging.getLogger("notes.share").debug("sentinel")
            share.verify_link(SECRET, link, now=0)
            for bad, now in ((flipped, 0), (link, 10**9), (link + "x", 0)):
                with self.assertRaises(Forbidden):
                    share.verify_link(SECRET, bad, now=now)
        text = "\n".join(cm.output)
        self.assertNotIn(sig, text)
        self.assertNotIn(flipped.split(".")[2], text)
        self.assertNotIn(link, text)
        self.assertNotIn(SECRET, text)

    def test_unit_out_監査と閲覧の出力に生の鍵がない_監査detailに本文がない(self):
        nid = service.create_note(self.conn, self.u["alice"], TITLE, BODY)
        service.update_note(self.conn, self.u["alice"], nid, 1, title=TITLE + "2", body=BODY + "2")
        service.delete_note(self.conn, self.u["alice"], nid)
        text = repr(service.export_audit(self.conn, self.u["admin"]))
        for s in (BODY, TITLE, support.KEY_ALICE):
            self.assertNotIn(s, text)
