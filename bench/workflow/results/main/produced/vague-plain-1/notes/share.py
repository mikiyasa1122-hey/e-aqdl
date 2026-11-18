import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden, ValidationError

log = logging.getLogger("notes.share")

_DIGITS = re.compile(r"[0-9]{1,18}\Z")
_SIG = re.compile(r"[0-9a-f]{64}\Z")


def _key(secret):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise ValidationError("secret must be non-empty")
    return bytes(secret)


def _sign(secret, note_id, exp):
    msg = ("%d:%d" % (note_id, exp)).encode("ascii")
    return hmac.new(_key(secret), msg, hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 0:
        raise ValidationError("invalid note_id")
    if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, (int, float)) or ttl_sec <= 0:
        raise ValidationError("ttl_sec must be positive")
    if now is None:
        now = time.time()
    exp = int(now + ttl_sec)
    return "%d.%d.%s" % (note_id, exp, _sign(secret, note_id, exp))


def verify_link(secret, link, now=None):
    _key(secret)
    if not isinstance(link, str):
        raise Forbidden("invalid link")
    parts = link.split(".")
    if len(parts) != 3 or not _DIGITS.match(parts[0]) or not _DIGITS.match(parts[1]) \
            or not _SIG.match(parts[2]):
        log.info("share link rejected: malformed")
        raise Forbidden("invalid link")
    note_id, exp = int(parts[0]), int(parts[1])
    if not hmac.compare_digest(_sign(secret, note_id, exp), parts[2]):
        log.info("share link rejected: bad signature")
        raise Forbidden("invalid link")
    if now is None:
        now = time.time()
    if now > exp:
        log.info("share link rejected: expired note=%d", note_id)
        raise Forbidden("link expired")
    return note_id
