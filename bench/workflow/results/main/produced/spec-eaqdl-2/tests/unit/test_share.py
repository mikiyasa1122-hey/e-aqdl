"""V19, V20: 共有リンク。"""
import hashlib
import hmac
import unittest

from notes import share
from notes.errors import Forbidden

SECRET = "dummy-share-secret"
NOW = 1_700_000_000


class MakeLinkTest(unittest.TestCase):
    def test_形式と署名(self) -> None:
        link = share.make_link(SECRET, 42, ttl_sec=100, now=NOW)
        nid, exp, sig = link.split(".")
        self.assertEqual((nid, exp), ("42", str(NOW + 100)))
        expected = hmac.new(SECRET.encode(), f"42:{NOW + 100}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)
        self.assertRegex(sig, r"\A[0-9a-f]{64}\Z")

    def test_既定のttlは3600秒_now小数は切り捨て(self) -> None:
        self.assertEqual(share.make_link(SECRET, 1, now=NOW + 0.9).split(".")[1], str(NOW + 3600))

    def test_nowを省略すると現在時刻基準(self) -> None:
        link = share.make_link(SECRET, 1, ttl_sec=3600)
        self.assertEqual(share.verify_link(SECRET, link), 1)

    def test_bytesのsecretも同じ署名(self) -> None:
        self.assertEqual(share.make_link(SECRET, 1, now=NOW), share.make_link(SECRET.encode(), 1, now=NOW))


class VerifyLinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.link = share.make_link(SECRET, 42, ttl_sec=100, now=NOW)
        self.exp = NOW + 100

    def test_有効(self) -> None:
        self.assertEqual(share.verify_link(SECRET, self.link, now=NOW), 42)
        self.assertEqual(share.verify_link(SECRET, self.link, now=self.exp - 1), 42)
        self.assertEqual(share.verify_link(SECRET, self.link, now=self.exp - 0.5), 42)

    def test_期限ちょうど以降は無効(self) -> None:
        for now in (self.exp, self.exp + 1, self.exp + 10**6):
            with self.subTest(now=now), self.assertRaises(Forbidden):
                share.verify_link(SECRET, self.link, now=now)

    def test_別secretは無効(self) -> None:
        with self.assertRaises(Forbidden):
            share.verify_link("another-secret", self.link, now=NOW)

    def test_note_idや期限の改ざんは無効(self) -> None:
        nid, exp, sig = self.link.split(".")
        for name, bad in [("note_id", f"43.{exp}.{sig}"), ("期限延長", f"{nid}.{self.exp + 1000}.{sig}"),
                          ("期限短縮", f"{nid}.{NOW + 1}.{sig}")]:
            with self.subTest(name), self.assertRaises(Forbidden):
                share.verify_link(SECRET, bad, now=NOW)


class InvalidLinkTest(unittest.TestCase):
    """V20: 不正リンクの表駆動。"""

    def test_不正リンクは全てForbidden(self) -> None:
        link = share.make_link(SECRET, 42, ttl_sec=100, now=NOW)
        nid, exp, sig = link.split(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        cases = {
            "署名1文字改ざん": f"{nid}.{exp}.{flipped}",
            "署名先頭改ざん": f"{nid}.{exp}." + ("0" if sig[0] != "0" else "1") + sig[1:],
            "大文字署名": f"{nid}.{exp}.{sig.upper()}" if sig != sig.upper() else f"{nid}.{exp}.{sig[:-1]}G",
            "署名63文字": f"{nid}.{exp}.{sig[:-1]}",
            "署名65文字": f"{nid}.{exp}.{sig}0",
            "署名が空": f"{nid}.{exp}.",
            "区切り2つ(署名なし)": f"{nid}.{exp}",
            "区切り4つ": f"{nid}.{exp}.{sig}.x",
            "区切りなし": nid + exp + sig,
            "note_idが数字以外": f"a.{exp}.{sig}",
            "期限が数字以外": f"{nid}.x.{sig}",
            "全角数字のnote_id": f"４２.{exp}.{sig}",
            "全角数字の期限": f"{nid}.{'１' * 10}.{sig}",
            "符号付き": f"+{nid}.{exp}.{sig}",
            "負の期限": f"{nid}.-{exp}.{sig}",
            "小数の期限": f"{nid}.{exp}.0.{sig}",
            "note_idが空": f".{exp}.{sig}",
            "期限が空": f"{nid}..{sig}",
            "末尾に改行": link + "\n",
            "先頭に空白": " " + link,
            "空文字": "",
            "16進以外の署名": f"{nid}.{exp}.{'g' * 64}",
        }
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(Forbidden):
                share.verify_link(SECRET, bad, now=NOW)

    def test_文字列以外はForbidden(self) -> None:
        for bad in (None, 123, b"1.2." + b"0" * 64, ["a"]):
            with self.subTest(repr(bad)), self.assertRaises(Forbidden):
                share.verify_link(SECRET, bad, now=NOW)  # type: ignore[arg-type]

    def test_メッセージは固定文(self) -> None:
        link = share.make_link(SECRET, 42, ttl_sec=100, now=NOW)
        msgs = set()
        for bad, now in [(link, NOW + 100), (link[:-1] + "0" if link[-1] != "0" else link[:-1] + "1", NOW), ("zzz", NOW)]:
            with self.assertRaises(Forbidden) as cm:
                share.verify_link(SECRET, bad, now=now)
            msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)
