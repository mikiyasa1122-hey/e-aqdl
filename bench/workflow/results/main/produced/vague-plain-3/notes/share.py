import hashlib
import hmac
import logging
import time

from .errors import Forbidden, ValidationError

log = logging.getLogger("notes.share")


def _key(secret):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise ValidationError("secret must be non-empty")
    return bytes(secret)


def _sign(secret, note_id, expires):
    return hmac.new(_key(secret), f"{note_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 0:
        raise ValidationError("invalid note_id")
    if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, (int, float)) or ttl_sec <= 0:
        raise ValidationError("ttl_sec must be positive")
    if now is None:
        now = time.time()
    expires = int(now + ttl_sec)
    link = f"{note_id}.{expires}.{_sign(secret, note_id, expires)}"
    log.info("share link created note=%s expires=%s", note_id, expires)
    return link


def verify_link(secret, link, now=None):
    key_ok = _key(secret)  # secret 不正は呼び出し側のバグとして ValidationError
    del key_ok
    if not isinstance(link, str):
        raise Forbidden("invalid link")
    parts = link.split(".")
    if len(parts) != 3:
        raise Forbidden("invalid link")
    nid, exp, sig = parts
    if not (nid.isascii() and nid.isdigit() and exp.isascii() and exp.isdigit()):
        raise Forbidden("invalid link")
    expected = _sign(secret, int(nid), int(exp))
    if not hmac.compare_digest(expected.encode(), sig.encode("utf-8", "replace")):
        log.warning("share link rejected: bad signature")
        raise Forbidden("invalid link")
    if now is None:
        now = time.time()
    if now > int(exp):
        log.info("share link rejected: expired note=%s", nid)
        raise Forbidden("link expired")
    return int(nid)
