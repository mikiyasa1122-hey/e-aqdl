"""V18 V19: 共有リンク。"""
import hashlib
import hmac
import unittest

from notes import share
from notes.errors import Forbidden, ValidationError

SECRET = "test-secret-" + "k3j9x"
NOW = 1_700_000_000


def expected_sig(secret: bytes, note_id: int, expiry: int) -> str:
    return hmac.new(secret, f"{note_id}:{expiry}".encode(), hashlib.sha256).hexdigest()


class ShareTest(unittest.TestCase):
    def test_share_形式と署名が契約どおり(self) -> None:
        link = share.make_link(SECRET, 42, ttl_sec=100, now=NOW)
        note_id, expiry, sig = link.split(".")
        self.assertEqual((note_id, expiry), ("42", str(NOW + 100)))
        self.assertEqual(sig, expected_sig(SECRET.encode(), 42, NOW + 100))
        self.assertRegex(sig, r"^[0-9a-f]{64}$")

    def test_share_bytesのsecretと小数のnow(self) -> None:
        link = share.make_link(SECRET.encode(), 1, ttl_sec=10, now=NOW + 0.9)
        self.assertEqual(link.split(".")[1], str(NOW + 10))
        self.assertEqual(share.verify_link(SECRET, link, now=NOW), 1)

    def test_share_往復でnote_idが戻る(self) -> None:
        for nid in (0, 1, 123456789):
            link = share.make_link(SECRET, nid, ttl_sec=3600, now=NOW)
            self.assertEqual(share.verify_link(SECRET, link, now=NOW), nid)
            self.assertEqual(share.verify_link(SECRET, link, now=NOW + 3599), nid)

    def test_share_既定のnowは現在時刻(self) -> None:
        link = share.make_link(SECRET, 7)
        self.assertEqual(share.verify_link(SECRET, link), 7)

    def test_share_期限ちょうどと後は無効(self) -> None:
        link = share.make_link(SECRET, 5, ttl_sec=60, now=NOW)
        self.assertEqual(share.verify_link(SECRET, link, now=NOW + 59.5), 5)
        for t in (NOW + 60, NOW + 61, NOW + 10**6):
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, link, now=t)

    def test_share_改ざんと別secretはForbidden(self) -> None:
        link = share.make_link(SECRET, 5, ttl_sec=60, now=NOW)
        nid, exp, sig = link.split(".")
        flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
        cases = [f"6.{exp}.{sig}", f"{nid}.{int(exp) + 1000}.{sig}", f"{nid}.{exp}.{flipped}",
                 f"{nid}.{exp}.{sig.upper()}", f"{nid}.{exp}.{sig[:-1]}", f"{nid}.{exp}.{sig}0"]
        for bad in cases:
            with self.assertRaises(Forbidden, msg=bad):
                share.verify_link(SECRET, bad, now=NOW)
        with self.assertRaises(Forbidden):
            share.verify_link(SECRET + "x", link, now=NOW)

    def test_share_形式不正は例外がForbiddenだけ(self) -> None:
        good_sig = "a" * 64
        cases: list[object] = ["", ".", "..", "...", "1", "1.2", "1.2.", "1..sig", f"1.2.{good_sig}.x", f".2.{good_sig}",
                 f"1..{good_sig}", f"a.2.{good_sig}", f"1.b.{good_sig}", f"-1.2.{good_sig}", f"1.-2.{good_sig}",
                 f"+1.2.{good_sig}", f"1.2.{'g' * 64}", f"{'9' * 400}.2.{good_sig}", f"1.{'9' * 400}.{good_sig}",
                 f"1.{'9' * 19}.{good_sig}", f"１.2.{good_sig}", f"1.２.{good_sig}", " 1.2." + good_sig,
                 f"1.2.{good_sig}\n", "\x00", "\ud800.1.2", None, 123, b"1.2.3", ["1", "2", "3"], {}]
        for bad in cases:
            with self.assertRaises(Forbidden, msg=repr(bad)):
                share.verify_link(SECRET, bad, now=NOW)

    def test_share_巨大数でも正しく署名されたものだけ通る(self) -> None:
        big = 10**17
        expiry = NOW + 10
        link = f"{big}.{expiry}.{expected_sig(SECRET.encode(), big, expiry)}"
        self.assertEqual(share.verify_link(SECRET, link, now=NOW), big)

    def test_share_設定ミスはValidationError(self) -> None:
        for secret in ("", b"", None, 5):
            with self.assertRaises(ValidationError):
                share.make_link(secret, 1)
            with self.assertRaises(ValidationError):
                share.verify_link(secret, "1.2." + "a" * 64)
        for kwargs in ({"note_id": -1}, {"note_id": True}, {"note_id": "1"}, {"note_id": 10**18},
                       {"note_id": 1, "ttl_sec": 0}, {"note_id": 1, "ttl_sec": -5}, {"note_id": 1, "ttl_sec": 1.5},
                       {"note_id": 1, "ttl_sec": True}):
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                share.make_link(SECRET, now=NOW, **kwargs)

    def test_share_ログに署名とsecretが出ない(self) -> None:
        link = share.make_link(SECRET, 5, ttl_sec=60, now=NOW)
        sig = link.split(".")[2]
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            share.verify_link(SECRET, link, now=NOW)
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, link, now=NOW + 60)
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, link[:-1] + ("0" if sig[-1] != "0" else "1"), now=NOW)
            with self.assertRaises(Forbidden):
                share.verify_link(SECRET, "broken", now=NOW)
        text = "\n".join(cm.output)
        for secret_text in (sig, SECRET, link):
            self.assertNotIn(secret_text, text)
        for kind in ("ok", "expired", "bad_signature", "bad_format"):
            self.assertIn(f"result={kind}", text)


if __name__ == "__main__":
    unittest.main()
