"""共有リンクのテスト（V19 V20 V35）。"""
import hashlib
import hmac
import logging
import unittest
from unittest import mock

from notes import share
from notes.errors import Forbidden, ValidationError

SECRET = "FAKE-SECRET-FOR-TEST"
NOW = 1_000_000


def _flip(ch):
    return "0" if ch != "0" else "1"


class MakeLinkTest(unittest.TestCase):
    def test_V19_形式と署名(self):
        link = share.make_link(SECRET, 42, ttl_sec=60, now=NOW)
        nid, exp, sig = link.split(".")
        self.assertEqual((nid, exp), ("42", str(NOW + 60)))
        expected = hmac.new(SECRET.encode(), ("42:%d" % (NOW + 60)).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)
        self.assertEqual(sig, sig.lower())
        self.assertEqual(len(sig), 64)

    def test_V19_期限の境界(self):
        link = share.make_link(SECRET, 7, ttl_sec=100, now=NOW)
        self.assertEqual(share.verify_link(SECRET, link, now=NOW), 7)  # 発行直後
        self.assertEqual(share.verify_link(SECRET, link, now=NOW + 99), 7)  # 1秒前
        self.assertEqual(share.verify_link(SECRET, link, now=NOW + 99.9), 7)
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, link, now=NOW + 100)  # ちょうど
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET, link, now=NOW + 101)  # 期限後

    def test_V19_secretはbytesでもよい(self):
        link = share.make_link(SECRET.encode(), 3, now=NOW)
        self.assertEqual(share.verify_link(SECRET, link, now=NOW), 3)

    def test_V19_nowを省略するとtime_timeを使う(self):
        with mock.patch("notes.share.time.time", return_value=5000.0):
            link = share.make_link(SECRET, 9, ttl_sec=10)
            self.assertEqual(link.split(".")[1], "5010")
            self.assertEqual(share.verify_link(SECRET, link), 9)
        with mock.patch("notes.share.time.time", return_value=5010.0):
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, link)

    def test_V19_発行側の不正引数はValidationError(self):
        cases = [dict(ttl_sec=0), dict(ttl_sec=-1), dict(ttl_sec="60"), dict(ttl_sec=None), dict(ttl_sec=True),
                 dict(secret=""), dict(secret=b""), dict(secret=None), dict(secret=5),
                 dict(note_id=0), dict(note_id=-1), dict(note_id="1"), dict(note_id=1.0), dict(note_id=True),
                 dict(note_id=None)]
        for override in cases:
            kwargs = dict(secret=SECRET, note_id=1, ttl_sec=60)
            kwargs.update(override)
            with self.subTest(override):
                with self.assertRaises(ValidationError):
                    share.make_link(kwargs["secret"], kwargs["note_id"], ttl_sec=kwargs["ttl_sec"], now=NOW)


class VerifyLinkTest(unittest.TestCase):
    def setUp(self):
        self.link = share.make_link(SECRET, 42, ttl_sec=1000, now=NOW)
        self.nid, self.exp, self.sig = self.link.split(".")

    def _assert_forbidden(self, secret, link):
        with self.assertRaises(Forbidden):
            share.verify_link(secret, link, now=NOW)

    def test_V20_各要素の改ざん(self):
        self._assert_forbidden(SECRET, "43.%s.%s" % (self.exp, self.sig))
        self._assert_forbidden(SECRET, "%s.%d.%s" % (self.nid, int(self.exp) + 1, self.sig))
        self._assert_forbidden(SECRET, "%s.%s.%s" % (self.nid, self.exp, _flip(self.sig[0]) + self.sig[1:]))
        self._assert_forbidden(SECRET, "%s.%s.%s" % (self.nid, self.exp, self.sig[:-1] + _flip(self.sig[-1])))
        self._assert_forbidden("OTHER-SECRET", self.link)

    def test_V20_不正形式(self):
        for bad in (None, "", "a.b", "1.2", "1.2.3.4", "x.%s.%s" % (self.exp, self.sig),
                    "%s.y.%s" % (self.nid, self.sig), "%s.%s." % (self.nid, self.exp),
                    "%s.%s.%s" % (self.nid, self.exp, self.sig.upper()), 123, b"1.2.3", ["1", "2", "3"],
                    self.link + "\n", " " + self.link, self.link + "0", "%s.%s.%s" % (self.nid, self.exp, self.sig[:-1]),
                    "-42.%s.%s" % (self.exp, self.sig), "+42.%s.%s" % (self.nid, self.sig),
                    "０.%s.%s" % (self.exp, self.sig), "%s.１０.%s" % (self.nid, self.sig)):
            with self.subTest(bad=bad):
                self._assert_forbidden(SECRET, bad)

    def test_V20_検証側のsecret不正もForbidden(self):
        for secret in (None, "", b"", 5, object()):
            with self.subTest(secret=secret):
                self._assert_forbidden(secret, self.link)

    def test_V20_大文字署名は拒否(self):
        self.assertEqual(share.verify_link(SECRET, self.link, now=NOW), 42)
        upper = "%s.%s.%s" % (self.nid, self.exp, self.sig.upper())
        if self.sig != self.sig.upper():
            self._assert_forbidden(SECRET, upper)

    def test_V35_note_idが0の署名つきリンクは拒否(self):
        exp = NOW + 100
        sig = hmac.new(SECRET.encode(), ("0:%d" % exp).encode(), hashlib.sha256).hexdigest()
        self._assert_forbidden(SECRET, "0.%d.%s" % (exp, sig))

    def test_V35_巨大な数字列でもForbiddenだけ(self):
        self._assert_forbidden(SECRET, "9" * 5000 + ".1." + self.sig)
        self._assert_forbidden(SECRET, "1." + "9" * 5000 + "." + self.sig)

    def test_V35_タイミング比較にcompare_digestを使う(self):
        with mock.patch("notes.share.hmac.compare_digest", wraps=hmac.compare_digest) as spy:
            self.assertEqual(share.verify_link(SECRET, self.link, now=NOW), 42)
        self.assertEqual(spy.call_count, 1)
        # 署名が正しい形式でも不一致なら比較関数を通って拒否される
        with mock.patch("notes.share.hmac.compare_digest", return_value=False) as spy:
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, self.link, now=NOW)
        self.assertEqual(spy.call_count, 1)

    def test_V35_多数の不正入力でForbidden以外の例外が出ない(self):
        junk = ["", ".", "..", "...", "a.b.c", "1.2.3", "1..", ".1.", "..1", "\x00.\x00.\x00", "1.2.%s" % ("g" * 64),
                "1.2.%s" % ("0" * 63), "1.2.%s" % ("0" * 65), "1.2.%s" % ("0" * 64), "🙂.🙂.🙂", "1.-1.%s" % ("a" * 64),
                "1.1e3.%s" % ("a" * 64), "1. 2.%s" % ("a" * 64), None, 0, 1.5, True, [], {}, object(), b"", b"a.b.c"]
        for i in range(len(self.link)):  # 1文字ずつ壊す
            junk.append(self.link[:i] + "#" + self.link[i + 1:])
            junk.append(self.link[:i])
        for link in junk:
            for secret in (SECRET, "x", None):
                try:
                    share.verify_link(secret, link, now=NOW)
                except Forbidden:
                    continue
                except Exception as exc:  # noqa
                    self.fail("Forbidden 以外: %r %r -> %r" % (secret, link, exc))
                # 例外が出ずに通ってよいのは正しいリンクと正しい secret だけ
                self.assertEqual((secret, link), (SECRET, self.link))

    def test_V35_改ざんの網羅_期限と署名の全桁(self):
        for i in range(len(self.sig)):
            tampered = self.sig[:i] + _flip(self.sig[i]) + self.sig[i + 1:]
            self._assert_forbidden(SECRET, "%s.%s.%s" % (self.nid, self.exp, tampered))
        for delta in (-1, 1, 1000, 10 ** 9):
            self._assert_forbidden(SECRET, "%s.%d.%s" % (self.nid, int(self.exp) + delta, self.sig))


class ShareLogTest(unittest.TestCase):
    def test_V23_notes_shareのログに署名もsecretも出ない(self):
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = H(logging.DEBUG)
        lg = logging.getLogger("notes.share")
        old_level = lg.level
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        self.addCleanup(lg.removeHandler, handler)
        self.addCleanup(lg.setLevel, old_level)
        link = share.make_link(SECRET, 5, now=NOW)
        sig = link.split(".")[2]
        share.verify_link(SECRET, link, now=NOW)
        for bad in (link + "x", "bad", None, link.replace(sig[:4], "0000")):
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, bad, now=NOW)
        text = "\n".join(records)
        for secret in (SECRET, sig, link):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
