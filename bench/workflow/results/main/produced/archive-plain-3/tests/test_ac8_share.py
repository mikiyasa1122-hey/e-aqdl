"""AC8・K8: R8 の規則どおりに共有リンクが検証される（期限の境界を含む）。"""
import unittest

from notes import share
from notes.errors import Forbidden


class ShareTest(unittest.TestCase):
    def setUp(self):
        self.link = share.make_link("s3cret", 7, ttl_sec=60, now=1000)

    def test_roundtrip_and_format(self):
        nid, exp, sig = self.link.split(".")
        self.assertEqual((nid, exp, len(sig)), ("7", "1060", 64))
        self.assertRegex(sig, r"^[0-9a-f]{64}$")
        self.assertEqual(share.verify_link("s3cret", self.link, now=1059), 7)

    def test_expiry_boundary(self):
        with self.assertRaises(Forbidden):
            share.verify_link("s3cret", self.link, now=1060)  # 期限の時刻ちょうどは無効
        with self.assertRaises(Forbidden):
            share.verify_link("s3cret", self.link, now=5000)

    def test_tampering(self):
        nid, exp, sig = self.link.split(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        bad = [f"8.{exp}.{sig}", f"{nid}.9999.{sig}", f"{nid}.{exp}.{flipped}"]
        for link in bad:
            with self.subTest(link=link[:20]), self.assertRaises(Forbidden):
                share.verify_link("s3cret", link, now=1001)
        with self.assertRaises(Forbidden):
            share.verify_link("other", self.link, now=1001)

    def test_malformed(self):
        nid, exp, sig = self.link.split(".")
        bad = ["", "7", "7.1060", f"{nid}.{exp}.{sig.upper()}", f"{nid}.{exp}.{sig}\n", f" {self.link}",
               f"-7.{exp}.{sig}", f"7.{exp}.{sig[:-1]}", f"7.{exp}.{sig}0", f"x.{exp}.{sig}", f"7.{exp}.{sig}.1",
               f"7.1e3.{sig}", "٧.1060." + sig, None, 7, b"7.1060.x"]
        for link in bad:
            with self.subTest(link=repr(link)[:30]), self.assertRaises(Forbidden):
                share.verify_link("s3cret", link, now=1001)
