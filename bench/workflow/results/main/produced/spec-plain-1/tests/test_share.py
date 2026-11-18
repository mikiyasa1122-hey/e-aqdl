import unittest

from notes.errors import Forbidden
from notes.share import make_link, verify_link


class ShareTests(unittest.TestCase):
    def test_roundtrip(self):
        link = make_link("s", 42, ttl_sec=100, now=1000)
        self.assertRegex(link, r"^42\.1100\.[0-9a-f]{64}$")
        self.assertEqual(verify_link("s", link, now=1000), 42)
        self.assertEqual(verify_link("s", link, now=1099), 42)

    def test_expiry_boundary(self):
        link = make_link("s", 1, ttl_sec=100, now=1000)
        with self.assertRaises(Forbidden):
            verify_link("s", link, now=1100)

    def test_invalid(self):
        link = make_link("s", 42, ttl_sec=100, now=1000)
        nid, exp, sig = link.split(".")
        bad = [link.replace("42.", "43.", 1), "%s.%d.%s" % (nid, int(exp) + 1, sig),
               link[:-1] + ("0" if link[-1] != "0" else "1"), link.upper(), link + "\n",
               nid + "." + exp, "", "a.b.c", "0" + link, "-1.1100." + sig, link + ".x"]
        for b in bad + [None, 5, b"x"]:
            with self.subTest(link=b), self.assertRaises(Forbidden):
                verify_link("s", b, now=1000)
        with self.assertRaises(Forbidden):
            verify_link("other", link, now=1000)

    def test_log_has_no_signature(self):
        link = make_link("s", 42, ttl_sec=1, now=0)
        sig = link.split(".")[2]
        with self.assertLogs("notes.share", "WARNING") as cm:
            with self.assertRaises(Forbidden):
                verify_link("s", link, now=5)
        self.assertNotIn(sig, "".join(cm.output))


if __name__ == "__main__":
    unittest.main()
