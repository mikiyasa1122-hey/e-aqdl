import re
import unittest

from notes.errors import Forbidden
from notes.share import make_link, verify_link

S = "secret"


class ShareTest(unittest.TestCase):
    def test_roundtrip_and_format(self):
        link = make_link(S, 42, ttl_sec=100, now=1000)
        self.assertRegex(link, r"^42\.1100\.[0-9a-f]{64}$")
        self.assertEqual(verify_link(S, link, now=1000), 42)
        self.assertEqual(verify_link(S, link, now=1099), 42)

    def test_expiry_boundary(self):
        link = make_link(S, 1, ttl_sec=100, now=1000)
        for now in (1100, 1101, 10**9):
            with self.subTest(now=now), self.assertRaises(Forbidden):
                verify_link(S, link, now=now)

    def test_default_now(self):
        self.assertEqual(verify_link(S, make_link(S, 5)), 5)
        with self.assertRaises(Forbidden):
            verify_link(S, make_link(S, 5, ttl_sec=-1))

    def test_tampered(self):
        link = make_link(S, 42, ttl_sec=100, now=1000)
        nid, exp, sig = link.split(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        bad = [f"43.{exp}.{sig}", f"{nid}.{int(exp) + 1000}.{sig}", f"{nid}.{exp}.{flipped}",
               f"{nid}.{exp}.{sig.upper()}", f"{nid}.{exp}.{sig[:-1]}", f"{nid}.{exp}.{sig}0",
               f"0{nid}.{exp}.{sig}"]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(Forbidden):
                verify_link(S, b, now=1000)
        with self.assertRaises(Forbidden):
            verify_link("other", link, now=1000)

    def test_malformed(self):
        sig = "a" * 64
        for b in ["", "abc", "1.2", "1.2.3.4", f"a.1.{sig}", f"1.b.{sig}", f"1.-2.{sig}",
                  f"1.2.{sig}\n", f" 1.2.{sig}", f"١.2.{sig}", f"1..{sig}", f".2.{sig}", None, 5, b"1.2.x"]:
            with self.subTest(b=b), self.assertRaises(Forbidden):
                verify_link(S, b, now=0)

    def test_no_signature_in_logs(self):
        link = make_link(S, 42, ttl_sec=1, now=0)
        sig = link.split(".")[2]
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            with self.assertRaises(Forbidden) as ctx:
                verify_link(S, link, now=10)
            self.assertNotIn(sig, str(ctx.exception))
            verify_link(S, make_link(S, 1, now=0), now=0)  # 成功
            with self.assertRaises(Forbidden):
                verify_link(S, link[:-1] + "x", now=0)
        self.assertNotIn(sig[:16], "\n".join(cm.output))
