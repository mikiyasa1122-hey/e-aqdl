"""期限付きの共有リンク（HMAC-SHA256）。署名・secret はログに出さない。"""
import hashlib
import hmac
import re
import time

from notes.errors import Forbidden, ValidationError


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _secret_bytes(secret):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or not secret:
        raise ValidationError("secret must be a non-empty str or bytes")
    return secret


def _sign(secret_bytes, note_id, exp):
    return hmac.new(secret_bytes, ("%d:%d" % (note_id, exp)).encode(), hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    secret_bytes = _secret_bytes(secret)
    if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id <= 0:
        raise ValidationError("note_id must be a positive integer")
    if not _is_number(ttl_sec) or not ttl_sec > 0:
        raise ValidationError("ttl_sec must be > 0")
    if now is None:
        now = time.time()
    exp = int(now + ttl_sec)
    return "%d.%d.%s" % (note_id, exp, _sign(secret_bytes, note_id, exp))


def verify_link(secret, link, now=None):
    """有効なら note_id を返す。無効な理由は区別せず、常に Forbidden だけを投げる。"""
    try:
        if not isinstance(link, str):
            raise Forbidden("invalid link")
        parts = link.split(".")
        if len(parts) != 3:
            raise Forbidden("invalid link")
        id_s, exp_s, sig = parts
        if not (re.fullmatch(r"[0-9]+", id_s, re.ASCII) and re.fullmatch(r"[0-9]+", exp_s, re.ASCII)
                and re.fullmatch(r"[0-9a-f]{64}", sig, re.ASCII)):
            raise Forbidden("invalid link")
        note_id, exp = int(id_s), int(exp_s)
        if note_id <= 0:
            raise Forbidden("invalid link")
        expected = _sign(_secret_bytes(secret), note_id, exp)
        if not hmac.compare_digest(expected.encode(), sig.encode()):
            raise Forbidden("invalid link")
        if now is None:
            now = time.time()
        if now >= exp:
            raise Forbidden("invalid link")
        return note_id
    except Forbidden:
        raise
    except Exception:
        raise Forbidden("invalid link") from None
