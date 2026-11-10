"""署名付き・期限付きの共有リンク。"""
import hashlib
import hmac
import logging
import re
import time

from notes.errors import Forbidden

logger = logging.getLogger("notes.share")

# \d や $ は使わない（全角数字・末尾改行を通さない）
_LINK_RE = re.compile(r"([0-9]{1,18})\.([0-9]{1,18})\.([0-9a-f]{64})")
_INVALID = "共有リンクが無効です"


def _sign(secret: str | bytes, note_id: int, exp: int) -> str:
    if isinstance(secret, str):
        key = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        key = secret
    else:
        raise TypeError("secret は str か bytes にしてください")
    return hmac.new(key, f"{note_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def make_link(secret: str | bytes, note_id: int, ttl_sec: int = 3600,
              now: float | None = None) -> str:
    if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id < 0:
        raise ValueError("note_id は0以上の整数にしてください")
    if not isinstance(ttl_sec, int) or isinstance(ttl_sec, bool):
        raise TypeError("ttl_sec は整数にしてください")
    base = time.time() if now is None else now
    exp = int(base) + ttl_sec
    if exp < 0:
        raise ValueError("期限が負になります")
    return f"{note_id}.{exp}.{_sign(secret, note_id, exp)}"


def verify_link(secret: str | bytes, link: str, now: float | None = None) -> int:
    m = _LINK_RE.fullmatch(link) if isinstance(link, str) else None
    if m is None:
        logger.info("共有リンクを拒否しました（形式不正）")
        raise Forbidden(_INVALID)
    note_id, exp = int(m.group(1)), int(m.group(2))
    # 署名検証を期限判定より先に行い、失敗理由を区別しない
    if not hmac.compare_digest(_sign(secret, note_id, exp), m.group(3)):
        logger.info("共有リンクを拒否しました note_id=%s", note_id)
        raise Forbidden(_INVALID)
    current = time.time() if now is None else now
    if current >= exp:
        logger.info("共有リンクを拒否しました note_id=%s", note_id)
        raise Forbidden(_INVALID)
    logger.debug("共有リンクを検証しました note_id=%s", note_id)
    return note_id
