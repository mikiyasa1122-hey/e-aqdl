"""署名付きで期限のある共有リンクの発行と検証。"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import time
from typing import Any

from .errors import Forbidden, ValidationError

logger = logging.getLogger("notes.share")

_INT64_MAX = 2 ** 63 - 1
_NUM = re.compile(r"[0-9]{1,19}")
_SIG = re.compile(r"[0-9a-f]{64}")


class _Reject(Exception):
    """検証失敗の内部用。reason はログ用の分類（format / signature / expired）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _secret_bytes(secret: Any) -> bytes:
    if isinstance(secret, str):
        try:
            data = secret.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("secret") from None
    elif isinstance(secret, bytes):
        data = secret
    else:
        raise TypeError("secret")
    if not data:
        raise ValueError("secret")
    return data


def _sign(secret: bytes, note_id: str, exp: str) -> str:
    return hmac.new(secret, (f"{note_id}:{exp}").encode("ascii"),
                    hashlib.sha256).hexdigest()


def _is_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def make_link(secret: Any, note_id: Any, ttl_sec: Any = 3600,
              now: float | None = None) -> str:
    try:
        key = _secret_bytes(secret)
    except (TypeError, ValueError):
        raise ValidationError("secret は空でない文字列か bytes で指定してください") from None
    if isinstance(note_id, bool) or not isinstance(note_id, int) or not 1 <= note_id <= _INT64_MAX:
        raise ValidationError("note_id は 1 以上の整数で指定してください")
    if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, int) or ttl_sec < 1:
        raise ValidationError("ttl_sec は 1 以上の整数で指定してください")
    if now is None:
        now = time.time()
    elif not _is_number(now):
        raise ValidationError("now は数値で指定してください")
    exp = int(now) + ttl_sec
    if not 1 <= exp <= _INT64_MAX:
        raise ValidationError("期限が範囲外です")
    sig = _sign(key, str(note_id), str(exp))
    logger.info("link issued note_id=%s exp=%s", note_id, exp)
    return f"{note_id}.{exp}.{sig}"


def _verify(secret: Any, link: Any, now: Any) -> int:
    if not isinstance(link, str):
        raise _Reject("format")
    parts = link.split(".")
    if len(parts) != 3:
        raise _Reject("format")
    note_s, exp_s, sig = parts
    for num in (note_s, exp_s):
        # ASCII 数字のみ・先頭 0 なし（表現を一意にする）
        if not _NUM.fullmatch(num) or str(int(num)) != num:
            raise _Reject("format")
    if not _SIG.fullmatch(sig):
        raise _Reject("format")
    try:
        key = _secret_bytes(secret)
    except (TypeError, ValueError):
        raise _Reject("format") from None
    expected = _sign(key, note_s, exp_s)
    if not hmac.compare_digest(expected, sig):
        raise _Reject("signature")
    current = time.time() if now is None else now
    if not current < int(exp_s):
        raise _Reject("expired")
    return int(note_s)


def verify_link(secret: Any, link: Any, now: float | None = None) -> int:
    """有効なら note_id を返す。無効なら理由を問わず Forbidden だけを出す。"""
    try:
        return _verify(secret, link, now)
    except _Reject as rej:
        logger.warning("link rejected reason=%s", rej.reason)
    except Exception:  # noqa: BLE001  Forbidden 以外を漏らさないため意図的に全て受ける
        logger.warning("link rejected reason=format")
    raise Forbidden("無効なリンクです")
