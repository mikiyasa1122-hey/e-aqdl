"""AC16 AC17 AC19: 共有リンク。"""
import hashlib
import hmac
import inspect
import re
import unittest

from notes import share
from notes.errors import Forbidden, ValidationError
from notes.share import make_link, verify_link

SECRET = "test-secret-dummy-value"
NOW = 1700000000


class ShareUnit(unittest.TestCase):
    def test_形式と署名(self):  # V16
        link = make_link(SECRET, 42, ttl_sec=100, now=NOW)
        note_id, exp, sig = link.split(".")
        self.assertEqual((note_id, exp), ("42", str(NOW + 100)))
        self.assertRegex(sig, r"^[0-9a-f]{64}$")
        expect = hmac.new(SECRET.encode(), f"42:{NOW + 100}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expect)

    def test_期限の前後とちょうど(self):  # V16
        link = make_link(SECRET, 7, ttl_sec=100, now=NOW)
        exp = NOW + 100
        self.assertEqual(verify_link(SECRET, link, now=NOW), 7)
        self.assertEqual(verify_link(SECRET, link, now=exp - 1), 7)
        self.assertEqual(verify_link(SECRET, link, now=exp - 0.001), 7)
        for t in (exp, exp + 1, exp + 10 ** 6):
            with self.subTest(now=t), self.assertRaises(Forbidden):
                verify_link(SECRET, link, now=t)

    def test_bytesのsecretと既定のnow(self):  # V16
        self.assertEqual(verify_link(b"bytes-secret", make_link(b"bytes-secret", 5)), 5)
        self.assertEqual(verify_link("日本語の秘密", make_link("日本語の秘密", 5)), 5)

    def test_既定のttlは1時間(self):  # V16
        exp = int(make_link(SECRET, 1, now=NOW).split(".")[1])
        self.assertEqual(exp, NOW + 3600)

    def test_無効なリンクはForbiddenのみ(self):  # V17 V30
        good = make_link(SECRET, 42, ttl_sec=100, now=NOW)
        nid, exp, sig = good.split(".")
        flip = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        other_exp = make_link(SECRET, 42, ttl_sec=200, now=NOW).split(".")[1]
        cases = [
            ("署名の改ざん", f"{nid}.{exp}.{flip}"),
            ("署名を大文字に", f"{nid}.{exp}.{sig.upper()}"),
            ("note_id改ざん", f"43.{exp}.{sig}"),
            ("期限改ざん", f"{nid}.{other_exp}.{sig}"),
            ("別secret", make_link("another-secret", 42, ttl_sec=100, now=NOW)),
            ("空文字", ""), ("区切りなし", "abc"), ("要素2つ", "1.2"), ("要素4つ", good + ".x"),
            ("署名が短い", f"{nid}.{exp}.{sig[:-1]}"),
            ("署名が長い", f"{nid}.{exp}.{sig}0"),
            ("符号付き", f"+{nid}.{exp}.{sig}"),
            ("先頭0", f"0{nid}.{exp}.{sig}"),
            ("アンダースコア", f"4_2.{exp}.{sig}"),
            ("全角数字", f"４２.{exp}.{sig}"),
            ("空白", f" {nid}.{exp}.{sig}"),
            ("改行", good + "\n"),
            ("note_id空", f".{exp}.{sig}"),
            ("数字が20桁", "9" * 20 + f".{exp}.{sig}"),
            ("None", None), ("int", 123), ("bytes", good.encode()), ("list", [good]), ("dict", {}),
            ("サロゲート", "\ud800.1.2"),
        ]
        for name, link in cases:
            with self.subTest(name), self.assertRaises(Forbidden):
                verify_link(SECRET, link, now=NOW)

    def test_secret不正のverifyもForbidden(self):  # V17
        link = make_link(SECRET, 1, now=NOW)
        for bad in ("", b"", None, 123, ["x"], "\ud800"):
            with self.subTest(secret=bad), self.assertRaises(Forbidden):
                verify_link(bad, link, now=NOW)

    def test_now不正でもForbidden(self):  # V17
        link = make_link(SECRET, 1, now=NOW)
        for bad in ("x", [1], float("nan")):
            with self.subTest(now=bad), self.assertRaises(Forbidden):
                verify_link(SECRET, link, now=bad)

    def test_makeの不正引数(self):  # V17
        cases = [{"secret": ""}, {"secret": b""}, {"secret": None}, {"secret": 1},
                 {"secret": "\ud800"}, {"note_id": 0}, {"note_id": -1}, {"note_id": "1"},
                 {"note_id": True}, {"note_id": 2 ** 63}, {"ttl_sec": 0}, {"ttl_sec": -5},
                 {"ttl_sec": 1.5}, {"ttl_sec": True}, {"ttl_sec": "9"}, {"now": "x"},
                 {"now": float("nan")}, {"ttl_sec": 2 ** 63}]
        for kw in cases:
            args = {"secret": SECRET, "note_id": 1, "ttl_sec": 10, "now": NOW}
            args.update(kw)
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                make_link(**args)

    def test_比較にcompare_digestを使う(self):  # V30
        src = inspect.getsource(share)
        self.assertIn("hmac.compare_digest", src)
        self.assertNotIsInstance(re.search(r"(sig|expected)\s*[!=]=\s*(sig|expected)", src), re.Match)

    def test_ログに署名とsecretを出さない(self):  # V19
        with self.assertLogs("notes.share", level="INFO") as cm:
            link = make_link(SECRET, 9, ttl_sec=60, now=NOW)
            verify_link(SECRET, link, now=NOW)
            for bad in (link[:-3] + "abc", "garbage", link, None):
                try:
                    verify_link(SECRET, bad, now=NOW + 1000)
                except Forbidden:
                    pass
        text = "\n".join(cm.output)
        sig = link.split(".")[2]
        self.assertNotIn(sig, text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(link, text)
        self.assertIn("link issued note_id=9", text)
        self.assertIn("reason=expired", text)
        self.assertIn("reason=format", text)

    def test_例外メッセージに署名とsecretを含めない(self):  # V32
        link = make_link(SECRET, 9, ttl_sec=60, now=NOW)
        with self.assertRaises(Forbidden) as cm:
            verify_link("wrong-secret-dummy", link, now=NOW)
        for s in (SECRET, "wrong-secret-dummy", link.split(".")[2], link):
            self.assertNotIn(s, str(cm.exception))
        with self.assertRaises(ValidationError) as cm2:
            make_link("", 1)
        self.assertNotIn("test-secret", str(cm2.exception))
