"""V8 / V18: R8 共有リンク（署名・期限）。"""
import hashlib
import hmac
import unittest

from notes import share
from notes.errors import Forbidden, ValidationError

SECRET = "dummy-secret-for-test"
NOW = 1_000_000


def sign(secret, nid, exp):
    return hmac.new(secret.encode(), f"{nid}:{exp}".encode(), hashlib.sha256).hexdigest()


class ShareTest(unittest.TestCase):
    def link(self, nid=42, ttl=100):
        return share.make_link(SECRET, nid, ttl_sec=ttl, now=NOW)

    def test_unit_share_往復でnote_idが戻る(self):
        for nid in (0, 1, 42, 10**12):
            self.assertEqual(share.verify_link(SECRET, self.link(nid), now=NOW), nid)
        self.assertIsInstance(share.verify_link(SECRET, self.link(), now=NOW), int)

    def test_unit_share_形式と署名が仕様どおり(self):
        link = self.link(42, 100)
        nid, exp, sig = link.split(".")
        self.assertEqual((nid, exp), ("42", str(NOW + 100)))
        self.assertEqual(sig, sign(SECRET, 42, NOW + 100))
        self.assertRegex(sig, r"^[0-9a-f]{64}$")

    def test_unit_share_期限の境界(self):
        link = self.link(ttl=100)
        exp = NOW + 100
        self.assertEqual(share.verify_link(SECRET, link, now=exp - 1), 42)
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, link, now=exp)
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, link, now=exp + 1)

    def test_unit_share_nowを省略すると現在時刻で期限が決まる(self):
        link = share.make_link(SECRET, 7, ttl_sec=3600)
        self.assertEqual(share.verify_link(SECRET, link), 7)
        expired = share.make_link(SECRET, 7, ttl_sec=1, now=1)
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, expired)

    def test_unit_share_改ざん・崩れたリンクは全てForbidden(self):
        link = self.link()
        nid, exp, sig = link.split(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        bad = [
            f"{nid}.{exp}.{flipped}",              # 署名 1 文字変更
            f"{nid}.{exp}.{sig.upper()}",          # 大文字化（英字を含む署名のとき）
            f"{nid}.{exp}.{sig[:-1]}",             # 短い
            f"{nid}.{exp}.{sig}0",                 # 長い
            f"43.{exp}.{sig}",                     # note_id 書き換え
            f"{nid}.{int(exp) + 1}.{sig}",         # 期限書き換え（延長）
            f"{nid}.{sig}", f"{nid}.{exp}", nid, "", "..",
            f"{nid}.{exp}.{sig}.x",                # 過剰
            f"{nid}.{exp}.{sig}\n",                # 末尾の改行
            f"\n{nid}.{exp}.{sig}",
            f" {nid}.{exp}.{sig}",
            f"+{nid}.{exp}.{sig}", f"-{nid}.{exp}.{sig}",
            f"{nid}.+{exp}.{sig}",
            f"４２.{exp}.{sig}",                   # 全角数字
            f"{nid}.{'１' * 6}.{sig}",
            f"{'9' * 5000}.{exp}.{sig}",           # 巨大な桁数
            f"{nid}.{'9' * 5000}.{sig}",
            None, 42, b"x", ["a"],
        ]
        for b in bad:
            with self.assertRaises(Forbidden, msg=repr(b)[:60]):
                share.verify_link(SECRET, b, now=NOW)
        with self.assertRaises(Forbidden):
            share.verify_link("other-secret", link, now=NOW)

    def test_unit_share_正しい署名でも全角数字の形式は拒否する(self):
        # 署名を全角数字のまま正しく計算しても、形式（半角の数字だけ）で拒否される
        exp = str(NOW + 100)
        wide = "４２"
        link = f"{wide}.{exp}.{sign(SECRET, wide, exp)}"
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, link, now=NOW)
        wide_exp = "".join(chr(ord("０") + int(c)) for c in exp)
        link = f"42.{wide_exp}.{sign(SECRET, 42, wide_exp)}"
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, link, now=NOW)

    def test_unit_share_大文字化は署名が英字を含む場合に拒否される(self):
        # 署名に英字 a-f が含まれるまで note_id を変えて探す（決定的）
        for nid in range(1000):
            link = self.link(nid)
            sig = link.split(".")[2]
            if sig != sig.upper():
                head = link.rsplit(".", 1)[0]
                with self.assertRaises(Forbidden):
                    share.verify_link(SECRET, f"{head}.{sig.upper()}", now=NOW)
                return
        self.fail("英字を含む署名が見つからない")

    def test_unit_share_失敗は同じメッセージで理由を明かさない(self):
        link = self.link()
        sig = link.split(".")[2]
        msgs = set()
        for bad, now in ((link + "x", NOW), (link.replace("42.", "43.", 1), NOW), (link, NOW + 1000)):
            with self.assertRaises(Forbidden) as cm:
                share.verify_link(SECRET, bad, now=now)
            msgs.add(str(cm.exception))
        self.assertEqual(msgs, {"invalid link"})
        self.assertNotIn(sig, "".join(msgs))

    def test_unit_share_make_linkの入力検査(self):
        for kw in ({"note_id": -1}, {"note_id": True}, {"note_id": "1"}, {"ttl_sec": 0},
                   {"ttl_sec": -5}, {"ttl_sec": True}, {"ttl_sec": 1.5}):
            args = {"note_id": 1, "ttl_sec": 10}
            args.update(kw)
            with self.assertRaises(ValidationError, msg=repr(kw)):
                share.make_link(SECRET, args["note_id"], ttl_sec=args["ttl_sec"], now=NOW)
        with self.assertRaises(ValueError):
            share.make_link("", 1, now=NOW)
