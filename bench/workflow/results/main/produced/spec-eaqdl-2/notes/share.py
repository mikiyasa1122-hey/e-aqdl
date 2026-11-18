"""署名付き・期限付きの共有リンク。"""
import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden

logger = logging.getLogger("notes.share")
logger.addHandler(logging.NullHandler())  # ライブラリとして未設定時の標準エラー出力を避ける

_LINK_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9a-f]{64}")  # ASCII のみ。fullmatch で使う
_INVALID_MSG = "共有リンクが無効です"


def _key(secret: str | bytes) -> bytes:
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def _sign(secret: str | bytes, note_id: str, expires: str) -> str:
    return hmac.new(_key(secret), f"{note_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def make_link(secret: str | bytes, note_id: int, ttl_sec: int = 3600, now: float | None = None) -> str:
    base = time.time() if now is None else now
    expires = int(base) + ttl_sec
    return f"{note_id}.{expires}.{_sign(secret, str(note_id), str(expires))}"


def _deny(reason: str) -> Forbidden:
    # 署名・リンク全体・secret は出さない。理由の区分だけ記録する
    logger.warning("share link rejected reason=%s", reason)
    return Forbidden(_INVALID_MSG)


def verify_link(secret: str | bytes, link: str, now: float | None = None) -> int:
    if not isinstance(link, str) or _LINK_RE.fullmatch(link) is None:
        raise _deny("format")
    note_id, expires, sig = link.split(".")
    if not hmac.compare_digest(_sign(secret, note_id, expires).encode(), sig.encode()):
        raise _deny("signature")
    current = time.time() if now is None else now
    if current >= int(expires):
        raise _deny("expired")
    return int(note_id)
