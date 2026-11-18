"""署名付き・期限付きの共有リンク。"""
import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden, ValidationError

logger = logging.getLogger("notes.share")

_LINK_RE = re.compile(r"([0-9]{1,30})\.([0-9]{1,30})\.([0-9a-f]{64})")


def _sign(secret, note_id_s, exp_s):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, ("%s:%s" % (note_id_s, exp_s)).encode("ascii"),
                    hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id < 0:
        raise ValidationError("invalid note id")
    if now is None:
        now = time.time()
    exp = int(now) + int(ttl_sec)
    if exp < 0:
        raise ValidationError("invalid expiry")
    return "%d.%d.%s" % (note_id, exp, _sign(secret, str(note_id), str(exp)))


def verify_link(secret, link, now=None):
    if now is None:
        now = time.time()
    m = _LINK_RE.fullmatch(link) if isinstance(link, str) else None
    if not m:
        logger.warning("share link rejected: malformed")
        raise Forbidden("invalid link")
    note_id_s, exp_s, sig = m.groups()
    if not hmac.compare_digest(sig, _sign(secret, note_id_s, exp_s)):
        logger.warning("share link rejected: bad signature")
        raise Forbidden("invalid link")
    if now >= int(exp_s):
        logger.warning("share link rejected: expired")
        raise Forbidden("invalid link")
    return int(note_id_s)
