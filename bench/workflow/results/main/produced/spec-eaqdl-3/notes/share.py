"""署名付き期限付きの共有リンク。"""
import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden

logger = logging.getLogger("notes.share")

_DIGITS = re.compile(r"[0-9]+", re.ASCII)
_SIG = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _sign(secret, note_id, exp):
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, f"{note_id}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if now is None:
        now = time.time()
    exp = int(now) + ttl_sec
    return f"{note_id}.{exp}.{_sign(secret, note_id, exp)}"


def _deny(reason):
    # 理由の種別だけを記録する（リンク・署名は出さない）
    logger.info("共有リンクを拒否 理由=%s", reason)
    return Forbidden("共有リンクが無効です")


def verify_link(secret, link, now=None):
    if not isinstance(link, str):
        raise _deny("形式")
    parts = link.split(".")
    if len(parts) != 3:
        raise _deny("形式")
    nid, exp, sig = parts
    if not (_DIGITS.fullmatch(nid) and _DIGITS.fullmatch(exp) and _SIG.fullmatch(sig)):
        raise _deny("形式")
    # 受け取った文字列のまま署名を再計算して比較する
    if not hmac.compare_digest(_sign(secret, nid, exp), sig):
        raise _deny("署名")
    if now is None:
        now = time.time()
    if now >= int(exp):
        raise _deny("期限")
    return int(nid)
