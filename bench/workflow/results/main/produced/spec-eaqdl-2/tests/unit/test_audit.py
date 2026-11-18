"""V15, V16: 監査ログ出力の伏せ字。"""
from notes import db, service
from notes.errors import Forbidden
from tests.helpers import API_KEY_A, API_KEY_B, SHORT_KEY, BaseCase


class MaskTest(BaseCase):
    def test_伏せ字の長さ規則(self) -> None:
        cases = {"": "", "a": "*", "abcdefg": "*******", "abcdefgh": "****efgh",
                 "0123456789": "******6789", "12345678901234567890": "*" * 16 + "7890"}
        for key, expected in cases.items():
            with self.subTest(len=len(key)):
                self.assertEqual(service._mask_key(key), expected)
        self.assertIsNone(service._mask_key(None))

    def test_8文字ちょうどと7文字(self) -> None:
        db.add_user(self.conn, 20, 1, "k7", "member", "1234567")
        db.add_user(self.conn, 21, 1, "k8", "member", "12345678")
        for uid in (20, 21):
            self.conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (1, ?, 'x', 'd')", (uid,))
        out = {r["actor"]: r["actor_key"] for r in service.export_audit(self.conn, self.admin)}
        self.assertEqual(out["k7"], "*******")
        self.assertEqual(out["k8"], "****5678")


class ExportAuditTest(BaseCase):
    def test_管理者のみ(self) -> None:
        for u in (self.alice, self.bob):
            with self.subTest(u["name"]), self.assertRaises(Forbidden):
                service.export_audit(self.conn, u)

    def test_自組織のログのみidの順(self) -> None:
        a = self.make(self.alice)
        self.make(self.carol)
        b = self.make(self.bob)
        out = service.export_audit(self.conn, self.admin)
        self.assertEqual([r["detail"] for r in out], [f"note={a}", f"note={b}"])
        self.assertEqual([r["id"] for r in out], sorted(r["id"] for r in out))
        self.assertEqual(set(out[0]), {"id", "action", "detail", "actor", "actor_key"})
        self.assertEqual((out[0]["actor"], out[0]["action"]), ("alice", "create"))
        self.assertEqual(out[0]["actor_key"], "*" * (len(API_KEY_B) - 4) + API_KEY_B[-4:])
        self.assertEqual(out[1]["actor_key"], "*" * len(SHORT_KEY))
        self.assertEqual(len(service.export_audit(self.conn, self.admin2)), 1)

    def test_出力全体に生のapi_keyが含まれない(self) -> None:
        self.make(self.alice)
        self.make(self.bob)
        self.make(self.admin)
        service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        text = repr(service.export_audit(self.conn, self.admin))
        for key in (API_KEY_A, API_KEY_B, SHORT_KEY):
            self.assertNotIn(key, text)
        # 伏せていない鍵の先頭部分も出ない
        self.assertNotIn(API_KEY_B[:-4], text)
        self.assertIn(API_KEY_B[-4:], text)
