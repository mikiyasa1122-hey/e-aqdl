"""V21, V22: ログ・例外・出力に機密（本文・署名・api_key）が出ない。"""
import logging
import unittest

from notes import service, share
from notes.errors import NotesError
from tests.helpers import API_KEY_A, API_KEY_B, BODY_SECRET, SHORT_KEY, BaseCase

SECRET = "dummy-share-secret"


class LogTest(BaseCase):
    def test_作成ログにメモidと作成者idがINFOで出る(self) -> None:
        with self.assertLogs("notes", level="INFO") as cm:
            nid = self.make(self.alice, title="題名ダミー", body=BODY_SECRET)
        recs = [r for r in cm.records if r.levelno == logging.INFO]
        self.assertEqual(len(recs), 1)
        msg = recs[0].getMessage()
        self.assertIn(f"id={nid}", msg)
        self.assertIn("owner=2", msg)

    def test_ログに本文題名api_keyが出ない(self) -> None:
        with self.assertLogs("notes", level="DEBUG") as cm:
            nid = self.make(self.alice, title="題名ダミー", body=BODY_SECRET, tags=["tg"])
            service.get_note(self.conn, self.alice, nid)
            service.update_note(self.conn, self.alice, nid, 1, body=BODY_SECRET + "2")
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
            service.export_audit(self.conn, self.admin)
            service.delete_note(self.conn, self.bob, nid)
        text = "\n".join(r.getMessage() for r in cm.records) + str([r.args for r in cm.records])
        for s in (BODY_SECRET, "題名ダミー", API_KEY_A, API_KEY_B, SHORT_KEY):
            self.assertNotIn(s, text)

    def test_失敗した作成はログに出ない(self) -> None:
        self.fail_at("create:tags")
        with self.assertNoLogs("notes", level="INFO"), self.assertRaises(RuntimeError):
            self.make(self.alice, body=BODY_SECRET)


class ExceptionTest(BaseCase):
    def test_業務例外のメッセージに機密が出ない(self) -> None:
        nid = self.make(self.alice, title="題名ダミー", body=BODY_SECRET, private=True)
        calls = [
            lambda: service.get_note(self.conn, self.bob, nid),
            lambda: service.update_note(self.conn, self.bob, nid, 1, title="x"),
            lambda: service.delete_note(self.conn, self.bob, nid),
            lambda: service.create_note(self.conn, self.alice, "", BODY_SECRET),
            lambda: service.create_note(self.conn, self.alice, "t", BODY_SECRET * 1000),
            lambda: service.create_note(self.conn, self.alice, "t", BODY_SECRET, tags=[API_KEY_A]),
            lambda: service.search_notes(self.conn, self.alice, "  ", tags=[BODY_SECRET]),
            lambda: service.update_note(self.conn, self.alice, nid, 99, body=BODY_SECRET),
            lambda: service.update_note(self.conn, self.alice, nid, 1, title=BODY_SECRET * 10),
            lambda: service.export_audit(self.conn, self.alice),
            lambda: service.transfer_notes(self.conn, self.alice, self.alice, self.bob),
        ]
        for i, fn in enumerate(calls):
            with self.subTest(i):
                with self.assertRaises(NotesError) as cm:
                    fn()
                text = str(cm.exception) + repr(cm.exception)
                for s in (BODY_SECRET, API_KEY_A, API_KEY_B, SHORT_KEY, "題名ダミー"):
                    self.assertNotIn(s, text)


class OutputTest(BaseCase):
    def test_export_auditの出力に本文と生の鍵が出ない(self) -> None:
        nid = self.make(self.alice, title="題名ダミー", body=BODY_SECRET)
        service.update_note(self.conn, self.alice, nid, 1, body=BODY_SECRET + "2")
        service.delete_note(self.conn, self.alice, nid)
        text = repr(service.export_audit(self.conn, self.admin))
        for s in (BODY_SECRET, "題名ダミー", API_KEY_B, SHORT_KEY):
            self.assertNotIn(s, text)

    def test_監査ログのDB内容にも本文が入らない(self) -> None:
        self.make(self.alice, title="題名ダミー", body=BODY_SECRET)
        text = repr([tuple(r) for r in self.conn.execute("SELECT * FROM audit")])
        self.assertNotIn(BODY_SECRET, text)
        self.assertNotIn("題名ダミー", text)


class ShareLogTest(unittest.TestCase):
    def test_拒否ログと例外に署名リンクsecretが出ない(self) -> None:
        link = share.make_link(SECRET, 42, ttl_sec=100, now=1000)
        sig = link.split(".")[2]
        tampered = link[:-1] + ("0" if link[-1] != "0" else "1")
        tsig = tampered.split(".")[2]
        for bad, now in [(link, 1100), (tampered, 1000), ("42.1100." + "A" * 64, 1000), ("junk", 1000)]:
            with self.subTest(bad[:12]):
                with self.assertLogs("notes.share", level="DEBUG") as cm, self.assertRaises(NotesError) as ex:
                    share.verify_link(SECRET, bad, now=now)
                text = "\n".join(r.getMessage() for r in cm.records) + str(ex.exception) + repr(ex.exception)
                for s in (sig, tsig, link, tampered, SECRET, bad):
                    self.assertNotIn(s, text)

    def test_成功時に署名やリンクをログに出さない(self) -> None:
        link = share.make_link(SECRET, 42, ttl_sec=100, now=1000)
        with self.assertNoLogs("notes.share", level="DEBUG"):
            share.verify_link(SECRET, link, now=1000)
