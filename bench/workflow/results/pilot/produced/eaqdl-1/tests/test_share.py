import logging
import unittest

from notes.errors import Forbidden
from notes.share import make_link, verify_link

SECRET = "dummy-secret-for-test"


class ShareTest(unittest.TestCase):
    def test_リンクの形は_id_期限_署名(self):
        link = make_link(SECRET, 42, ttl_sec=60, now=1000)
        nid, exp, sig = link.split(".")
        self.assertEqual((nid, exp), ("42", "1060"))
        self.assertRegex(sig, r"\A[0-9a-f]{64}\Z")
        import hashlib
        import hmac
        self.assertEqual(sig, hmac.new(SECRET.encode(), b"42:1060", hashlib.sha256).hexdigest())

    def test_有効なリンクはnote_idを返す_bytesの鍵も使える(self):
        self.assertEqual(verify_link(SECRET, make_link(SECRET, 7, now=1000), now=1000), 7)
        self.assertEqual(verify_link(b"bin-key", make_link(b"bin-key", 0, now=5), now=5), 0)

    def test_期限の境界(self):
        link = make_link(SECRET, 7, ttl_sec=60, now=1000)  # 期限 1060
        self.assertEqual(verify_link(SECRET, link, now=1059), 7)
        self.assertEqual(verify_link(SECRET, link, now=1059.999), 7)
        for now in (1060, 1060.0, 1061, 10**9):
            with self.assertRaises(Forbidden, msg=str(now)):
                verify_link(SECRET, link, now=now)

    def test_nowの小数は切り捨てて期限を作る(self):
        link = make_link(SECRET, 1, ttl_sec=10, now=100.9)
        self.assertEqual(link.split(".")[1], "110")

    def test_ttl0は作った時刻で既に無効(self):
        link = make_link(SECRET, 1, ttl_sec=0, now=500)
        with self.assertRaises(Forbidden):
            verify_link(SECRET, link, now=500)

    def test_改ざんは全てForbidden(self):
        link = make_link(SECRET, 7, ttl_sec=60, now=1000)
        nid, exp, sig = link.split(".")
        flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
        flipped_last = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        tampered = [
            f"8.{exp}.{sig}",            # note_id の書き換え
            f"{nid}.{int(exp) + 1}.{sig}",  # 期限の延長
            f"{nid}.{exp}.{flipped}",
            f"{nid}.{exp}.{flipped_last}",
            f"{nid}.{exp}.{sig.upper()}",  # 大文字は形式違反
        ]
        for t in tampered:
            with self.assertRaises(Forbidden, msg=t):
                verify_link(SECRET, t, now=1000)
        with self.assertRaises(Forbidden):
            verify_link("other-secret", link, now=1000)

    def test_形の崩れたリンクはForbidden(self):
        link = make_link(SECRET, 7, ttl_sec=60, now=1000)
        nid, exp, sig = link.split(".")
        bad = ["", ".", "..", "abc", f"{nid}.{exp}", f"{nid}.{exp}.{sig}.x", f"{nid}.{exp}.{sig[:-1]}",
               f"{nid}.{exp}.{sig}0", f"{nid}.{exp}.{sig}\n", f"{link} ", f" {link}", f"{link}\n",
               f"-{nid}.{exp}.{sig}", f"+{nid}.{exp}.{sig}", f"{nid}.-{exp}.{sig}",
               f"７.{exp}.{sig}",  # 全角数字
               f"{nid}.{exp}.{'g' * 64}", f"{nid}.{exp}.{'０' * 64}",
               f"{'9' * 19}.{exp}.{sig}", f"{nid}.{'9' * 19}.{sig}",
               f"{nid}..{sig}", f".{exp}.{sig}", f"0x7.{exp}.{sig}", f"7.1e3.{sig}"]
        for b in bad:
            with self.assertRaises(Forbidden, msg=repr(b)):
                verify_link(SECRET, b, now=1000)
        for b in (None, 123, b"x.y.z", ["a"]):
            with self.assertRaises(Forbidden):
                verify_link(SECRET, b, now=1000)  # type: ignore[arg-type]

    def test_期限切れと改ざんは同じ文言(self):
        link = make_link(SECRET, 7, ttl_sec=60, now=1000)
        _, exp, sig = link.split(".")
        msgs = set()
        for t, now in ((link, 2000), (f"8.{exp}.{sig}", 1000), ("zzz", 1000)):
            with self.assertRaises(Forbidden) as cm:
                verify_link(SECRET, t, now=now)
            msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)
        m = msgs.pop()
        self.assertNotIn(sig, m)

    def test_改ざんかつ期限切れでも署名不一致で拒否(self):
        link = make_link(SECRET, 7, ttl_sec=60, now=1000)
        _, exp, sig = link.split(".")
        with self.assertRaises(Forbidden):
            verify_link(SECRET, f"9.{exp}.{sig}", now=0)

    def test_makeの引数の不正(self):
        for kw in ({"note_id": -1}, {"note_id": True}, {"note_id": "1"}, {"note_id": 1, "ttl_sec": -100000}):
            with self.assertRaises(ValueError, msg=repr(kw)):
                make_link(SECRET, now=1000, **kw)
        with self.assertRaises(TypeError):
            make_link(123, 1, now=1000)  # type: ignore[arg-type]

    def test_ログに署名もリンクも出ない(self):
        link = make_link(SECRET, 7, ttl_sec=60, now=1000)
        sig = link.split(".")[2]
        bad = link[:-1] + ("0" if link[-1] != "0" else "1")
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            verify_link(SECRET, link, now=1000)
            for t, now in ((bad, 1000), (link, 5000), ("zzz", 1000)):
                with self.assertRaises(Forbidden):
                    verify_link(SECRET, t, now=now)
        out = "\n".join(cm.output)
        self.assertEqual(len(cm.records), 4)
        self.assertNotIn(sig, out)
        self.assertNotIn(bad.split(".")[2], out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("zzz", out)
        self.assertIn("note_id=7", out)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
