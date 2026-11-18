import hashlib
import hmac
import unittest

from notes.errors import Forbidden
from notes.share import make_link, verify_link


class ShareTests(unittest.TestCase):
    def test_format_and_roundtrip(self):
        link = make_link("s3cret", 42, ttl_sec=100, now=1000)
        nid, exp, sig = link.split(".")
        self.assertEqual((nid, exp), ("42", "1100"))
        self.assertEqual(sig, hmac.new(b"s3cret", b"42:1100", hashlib.sha256).hexdigest())
        self.assertEqual(verify_link("s3cret", link, now=1000), 42)

    def test_expiry_boundary(self):
        link = make_link("k", 1, ttl_sec=100, now=1000)
        self.assertEqual(verify_link("k", link, now=1099), 1)
        for now in (1100, 1101):
            with self.assertRaises(Forbidden):
                verify_link("k", link, now=now)

    def test_tamper_and_malformed(self):
        link = make_link("k", 1, ttl_sec=100, now=1000)
        nid, exp, sig = link.split(".")
        bad = [
            "2.%s.%s" % (exp, sig), "%s.9999.%s" % (nid, sig),
            "%s.%s.%s" % (nid, exp, sig[:-1] + ("0" if sig[-1] != "0" else "1")),
            "%s.%s.%s" % (nid, exp, sig.upper()), "%s.%s.%s" % (nid, exp, sig[:-1]),
            "%s.%s.%s0" % (nid, exp, sig), link + "\n", " " + link, "", "abc", "1.2", "a.b.c",
            "-1.%s.%s" % (exp, sig), "%s.%s.%s" % ("١", exp, sig), None, 5, "%s..%s" % (nid, sig),
        ]
        for b in bad:
            with self.assertRaises(Forbidden, msg=repr(b)):
                verify_link("k", b, now=1000)
        with self.assertRaises(Forbidden):
            verify_link("other", link, now=1000)

    def test_log_has_no_signature(self):
        link = make_link("k", 1, ttl_sec=10, now=0)
        sig = link.split(".")[2]
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            with self.assertRaises(Forbidden) as ctx:
                verify_link("k", link, now=50)
        self.assertNotIn(sig, "\n".join(cm.output) + str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
