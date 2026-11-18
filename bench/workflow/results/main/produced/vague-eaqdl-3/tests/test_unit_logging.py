"""V25 V26: ログに秘密を出さない。"""
from notes import service
from tests.helpers import NotesTestCase


class LoggingTest(NotesTestCase):
    def test_log_createはidと作成者idだけ(self) -> None:
        body = "本文の秘密-" + "q7w8"
        with self.assertLogs("notes", level="INFO") as cm:
            nid = service.create_note(self.conn, self.alice, "題名の秘密", body, tags=["tagsecret"])
        self.assertEqual(len(cm.records), 1)
        record = cm.records[0]
        self.assertEqual(record.levelname, "INFO")
        self.assertEqual(record.getMessage(), f"note created id={nid} owner={self.alice['id']}")
        self.assertEqual(record.args, (nid, self.alice["id"]))

    def test_log_全操作のログに秘密が含まれない(self) -> None:
        body = "本文の秘密-q7w8"
        with self.assertLogs("notes", level="DEBUG") as cm:
            nid = service.create_note(self.conn, self.alice, "題名の秘密", body, tags=["tagsecret"])
            service.get_note(self.conn, self.alice, nid)
            service.list_notes(self.conn, self.alice)
            service.search_notes(self.conn, self.alice, "秘密")
            service.update_note(self.conn, self.alice, nid, 1, body=body + "更新")
            service.transfer_notes(self.conn, self.carol, self.alice, self.bob)
            service.export_audit(self.conn, self.carol)
            service.delete_note(self.conn, self.bob, nid)
        text = "\n".join(r.getMessage() + " " + str(r.args) for r in cm.records)
        for secret in (body, "題名の秘密", "tagsecret", self.alice["api_key"], self.carol["api_key"],
                       self.bob["api_key"], "api_key"):
            self.assertNotIn(secret, text)
