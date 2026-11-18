"""V6 V18 V8 V19: 監査ログの伏せ字と共有リンク。"""
import unittest

from notes import db, service, share
from notes.errors import Forbidden
from tests.helpers import DbTestCase

SECRET = "テスト用のダミー秘密"
NOTE = 42
NOW = 1_000_000


class AuditExport(DbTestCase):
    def _actor_key(self, key):
        u = db.add_user(self.conn, 100 + len(key), 1, "u%d" % len(key), "member", key)
        service.create_note(self.conn, u, "t", "b")
        rows = [r for r in service.export_audit(self.conn, self.admin) if r["actor"] == u["name"]]
        return rows[0]["actor_key"]

    def test_鍵長の境界(self):
        self.assertEqual(self._actor_key("abcdefg"), "*******")       # 7文字は全て伏せる
        self.assertEqual(self._actor_key("abcdefgh"), "****efgh")     # 8文字は末尾4のみ
        self.assertEqual(self._actor_key("k" * 3), "***")
        self.assertEqual(self._actor_key("sk-live-ABCDEF123456"), "*" * 16 + "3456")

    def test_管理者のみ(self):
        service.create_note(self.conn, self.alice, "t", "b")
        for u in (self.alice, self.bob):
            with self.assertRaises(Forbidden):
                service.export_audit(self.conn, u)
        self.assertEqual(len(service.export_audit(self.conn, self.admin)), 1)

    def test_他組織の記録は出ない_全文も出ない(self):
        service.create_note(self.conn, self.alice, "t", "b")
        service.create_note(self.conn, self.eve, "t", "b")
        rows = service.export_audit(self.conn, self.admin)
        self.assertEqual([r["actor"] for r in rows], ["alice"])
        self.assertEqual(rows[0]["action"], "create")
        self.assertNotIn(self.alice["api_key"], repr(rows))
        self.assertEqual(rows[0]["actor_key"], "*" * 10 + "0001")


class ShareLink(unittest.TestCase):
    def link(self, ttl=100):
        return share.make_link(SECRET, NOTE, ttl_sec=ttl, now=NOW)

    def test_往復(self):
        self.assertEqual(share.verify_link(SECRET, self.link(), now=NOW), NOTE)
        self.assertEqual(share.verify_link(SECRET, self.link(), now=NOW + 99), NOTE)
        self.assertEqual(share.verify_link(SECRET.encode(), self.link(), now=NOW), NOTE)

    def test_形式(self):
        nid, exp, sig = self.link().split(".")
        self.assertEqual((nid, exp), (str(NOTE), str(NOW + 100)))
        self.assertRegex(sig, r"^[0-9a-f]{64}$")

    def test_期限の境界(self):
        l = self.link()
        share.verify_link(SECRET, l, now=NOW + 99.9)
        for now in (NOW + 100, NOW + 101, NOW + 10_000):
            with self.subTest(now=now), self.assertRaises(Forbidden):
                share.verify_link(SECRET, l, now=now)

    def test_改ざんと形崩れは全てForbidden(self):
        nid, exp, sig = self.link().split(".")
        bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
        cases = {
            "署名改ざん": f"{nid}.{exp}.{bad_sig}",
            "期限改ざん": f"{nid}.{int(exp) + 1000}.{sig}",
            "id改ざん": f"{NOTE + 1}.{exp}.{sig}",
            "署名63桁": f"{nid}.{exp}.{sig[:-1]}",
            "署名65桁": f"{nid}.{exp}.{sig}0",
            "大文字16進": f"{nid}.{exp}.{sig.upper()}",
            "非数字id": f"x.{exp}.{sig}",
            "非数字期限": f"{nid}.x.{sig}",
            "全角数字": f"{nid}.{'１２３'}.{sig}",
            "先頭符号": f"+{nid}.{exp}.{sig}",
            "要素2": f"{nid}.{exp}",
            "要素4": f"{nid}.{exp}.{sig}.x",
            "空": "",
            "末尾改行": f"{nid}.{exp}.{sig}\n",
            "先頭空白": f" {nid}.{exp}.{sig}",
        }
        for name, l in cases.items():
            with self.subTest(name), self.assertRaises(Forbidden):
                share.verify_link(SECRET, l, now=NOW)
        for l in (None, 123, b"1.2.3"):
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, l, now=NOW)

    def test_別secretは拒否(self):
        with self.assertRaises(Forbidden):
            share.verify_link("別のダミー秘密", self.link(), now=NOW)

    def test_同一メッセージ(self):
        msgs = set()
        for l in ("x", self.link(), self.link()[:-1] + "0"):
            try:
                share.verify_link(SECRET, l, now=NOW + 1000 if l == self.link() else NOW)
            except Forbidden as e:
                msgs.add(str(e))
        self.assertEqual(len(msgs), 1)


class ShareLinkFormat(unittest.TestCase):
    """秘密で正しく署名されていても、形式が崩れていれば拒否する（形式検査の独立した確認）。"""

    def test_正しい署名でも形式違反は拒否(self):
        exp = NOW + 100
        for nid in ("+42", "４２", " 42", "42\n", "-1", "0x2a", "", "4_2"):
            with self.subTest(nid=nid), self.assertRaises(Forbidden):
                share.verify_link(SECRET, f"{nid}.{exp}.{share._sign(SECRET, nid, exp)}", now=NOW)
        for e in ("+9999999", "９９９９９９９", "1e9", "9999999\n"):
            with self.subTest(exp=e), self.assertRaises(Forbidden):
                share.verify_link(SECRET, f"{NOTE}.{e}.{share._sign(SECRET, NOTE, e)}", now=NOW)

    def test_署名の大文字と非ASCIIは拒否(self):
        exp = NOW + 100
        sig = share._sign(SECRET, NOTE, exp)
        for bad in (sig.upper().replace("0", "A"), sig[:-1] + "é", sig + "\n"):
            with self.subTest(bad=bad), self.assertRaises(Forbidden):
                share.verify_link(SECRET, f"{NOTE}.{exp}.{bad}", now=NOW)
