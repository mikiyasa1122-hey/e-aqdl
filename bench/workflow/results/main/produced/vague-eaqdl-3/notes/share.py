"""期限付き共有リンク（HMAC-SHA256 署名）。

リンクの形式: "<note_id>.<期限>.<署名>"。署名は "<note_id>:<期限>" の HMAC-SHA256 の小文字16進。
ログには結果の種類と note_id だけを出し、署名・secret・リンク全文は出さない。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from typing import Any

from notes.errors import Forbidden, ValidationError

logger = logging.getLogger("notes.share")

_NUM = re.compile(r"[0-9]{1,18}")
_SIG = re.compile(r"[0-9a-f]{64}")
_MAX_NUM = 10**18


def _secret_bytes(secret: Any) -> bytes:
    if isinstance(secret, str):
        data = secret.encode("utf-8", errors="strict") if secret else b""
    elif isinstance(secret, bytes):
        data = secret
    else:
        raise ValidationError("secret は文字列かバイト列で指定してください")
    if not data:
        raise ValidationError("secret が空です")
    return data


def _sign(secret: bytes, note_id: int, expiry: int) -> str:
    return hmac.new(secret, f"{note_id}:{expiry}".encode(), hashlib.sha256).hexdigest()


def _check_now(now: Any) -> float:
    if now is None:
        return time.time()
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValidationError("now は数値で指定してください")
    return now


def make_link(secret: Any, note_id: Any, ttl_sec: Any = 3600, now: Any = None) -> str:
    try:
        key = _secret_bytes(secret)
    except UnicodeEncodeError:
        raise ValidationError("secret に不正な文字が含まれています") from None
    if isinstance(note_id, bool) or not isinstance(note_id, int) or not 0 <= note_id < _MAX_NUM:
        raise ValidationError("note_id が不正です")
    if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, int) or ttl_sec <= 0:
        raise ValidationError("ttl_sec は正の整数で指定してください")
    expiry = int(_check_now(now)) + ttl_sec
    if not 0 <= expiry < _MAX_NUM:
        raise ValidationError("期限が範囲外です")
    return f"{note_id}.{expiry}.{_sign(key, note_id, expiry)}"


def verify_link(secret: Any, link: Any, now: Any = None) -> int:
    """リンクを検証して note_id を返す。無効なら Forbidden だけを出す。"""
    try:
        key = _secret_bytes(secret)
    except UnicodeEncodeError:
        raise ValidationError("secret に不正な文字が含まれています") from None
    current = _check_now(now)
    parts = link.split(".") if isinstance(link, str) else []
    if (len(parts) != 3 or not _NUM.fullmatch(parts[0]) or not _NUM.fullmatch(parts[1])
            or not _SIG.fullmatch(parts[2])):
        logger.debug("share link result=bad_format")
        raise Forbidden("共有リンクが無効です")
    note_id, expiry = int(parts[0]), int(parts[1])
    if not hmac.compare_digest(_sign(key, note_id, expiry), parts[2]):
        logger.debug("share link result=bad_signature")
        raise Forbidden("共有リンクが無効です")
    if current >= expiry:
        logger.debug("share link result=expired note_id=%s", note_id)
        raise Forbidden("共有リンクが無効です")
    logger.debug("share link result=ok note_id=%s", note_id)
    return note_id
