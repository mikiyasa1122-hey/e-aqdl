"""署名付き・期限付きの共有リンク。形式は note_id.期限.署名。"""
import hashlib
import hmac
import logging
import re
import time

from notes.errors import Forbidden, ValidationError

logger = logging.getLogger("notes.share")
logger.addHandler(logging.NullHandler())  # ライブラリとして、呼び出し側が設定するまで出力しない

_LINK_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9a-f]{64}")


def _sign(secret: str | bytes, note_id: str, exp: str) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not key:
        raise ValueError("secret must not be empty")
    return hmac.new(key, f"{note_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None) -> str:
    if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 0:
        raise ValidationError("invalid note_id")
    if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, int) or ttl_sec < 1:
        raise ValidationError("invalid ttl")
    base = time.time() if now is None else now
    exp = str(int(base) + ttl_sec)
    nid = str(note_id)
    return f"{nid}.{exp}.{_sign(secret, nid, exp)}"


def _invalid(reason: str) -> Forbidden:
    # 失敗の分類だけを記録する（リンク・署名は出さない）
    logger.warning("share link rejected reason=%s", reason)
    return Forbidden("invalid link")


def verify_link(secret, link, now=None) -> int:
    if not isinstance(link, str) or _LINK_RE.fullmatch(link) is None:
        raise _invalid("format")
    nid, exp, sig = link.split(".")
    if not hmac.compare_digest(_sign(secret, nid, exp), sig):
        raise _invalid("signature")
    try:
        exp_int = int(exp)
        note_id = int(nid)
    except ValueError:
        raise _invalid("format") from None
    current = time.time() if now is None else now
    if current >= exp_int:
        raise _invalid("expired")
    return note_id
