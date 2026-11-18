"""V9 V20: ログ・例外・出力に機密を出さない。"""
import re
from notes import service, share
from notes.errors import Forbidden, NotesError
from tests.helpers import DbTestCase

BODY = "本文の機密テキスト-ZZZ-9981"
TITLE = "題名の機密-QQQ"
SECRET = "共有用ダミー秘密"


class Secrets(DbTestCase):
    def test_作成ログにidが出て本文は出ない(self):
        with self.assertLogs("notes", level="INFO") as cm:
            nid = service.create_note(self.conn, self.alice, TITLE, BODY, tags=["tag-x"])
        out = "\n".join(cm.output)
        self.assertIn(f"note_id={nid}", out)
        self.assertIn(f"user_id={self.alice['id']}", out)
        for s in (BODY, TITLE, "tag-x", self.alice["api_key"]):
            self.assertNotIn(s, out)

    def test_例外メッセージに入力値が出ない(self):
        nid = service.create_note(self.conn, self.alice, TITLE, BODY, private=True)
        calls = [
            lambda: service.create_note(self.conn, self.alice, " ", BODY),
            lambda: service.create_note(self.conn, self.alice, TITLE, BODY * 1000),
            lambda: service.create_note(self.conn, self.alice, TITLE, BODY, tags=["BAD-" + BODY]),
            lambda: service.search_notes(self.conn, self.alice, " "),
            lambda: service.search_notes(self.conn, self.alice, "q", tags=["X" + BODY]),
            lambda: service.get_note(self.conn, self.bob, nid),
            lambda: service.update_note(self.conn, self.alice, nid, 99, title=TITLE),
            lambda: service.update_note(self.conn, self.alice, nid, 1, body=BODY * 1000),
            lambda: service.update_note(self.conn, self.bob, nid, 1, title=TITLE),
            lambda: service.export_audit(self.conn, self.alice),
            lambda: service.transfer_notes(self.conn, self.alice, self.alice, self.bob),
            lambda: service.transfer_notes(self.conn, self.admin, self.alice, self.eve),
        ]
        for i, c in enumerate(calls):
            with self.subTest(i=i), self.assertRaises(NotesError) as cm:
                c()
            msg = str(cm.exception)
            for s in (BODY, TITLE, self.alice["api_key"], self.bob["api_key"]):
                self.assertNotIn(s, msg)

    def test_監査出力に本文と鍵全文が無い(self):
        service.create_note(self.conn, self.alice, TITLE, BODY)
        out = repr(service.export_audit(self.conn, self.admin))
        for u in (self.alice, self.bob, self.admin):
            self.assertNotIn(u["api_key"], out)
        self.assertNotIn(BODY, out)

    def test_共有リンクのログと例外に署名が出ない(self):
        link = share.make_link(SECRET, 7, now=1000)
        sig = link.split(".")[2]
        tampered = link[:-1] + ("0" if link[-1] != "0" else "1")
        tsig = tampered.split(".")[2]
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            for l, now in ((link, 5000), (tampered, 1000), ("x.y", 1000), (link.upper(), 1000)):
                with self.assertRaises(Forbidden) as ex:
                    share.verify_link(SECRET, l, now=now)
                for s in (sig, tsig, SECRET):
                    self.assertNotIn(s, str(ex.exception))
        out = "\n".join(cm.output)
        self.assertTrue(cm.output)
        for s in (sig, tsig, SECRET, link):
            self.assertNotIn(s, out)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", out))
