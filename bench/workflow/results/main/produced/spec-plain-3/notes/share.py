import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden

log = logging.getLogger("notes.share")

_LINK_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9a-f]{64})")


def _sign(secret, note_id, expires):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = ("%s:%s" % (note_id, expires)).encode("ascii")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if now is None:
        now = time.time()
    expires = int(now) + int(ttl_sec)
    return "%d.%d.%s" % (note_id, expires, _sign(secret, note_id, expires))


def verify_link(secret, link, now=None):
    if now is None:
        now = time.time()
    m = _LINK_RE.fullmatch(link) if isinstance(link, str) else None
    if m is None:
        log.info("share link rejected: malformed")
        raise Forbidden("invalid link")
    note_id_s, expires_s, sig = m.groups()
    if not hmac.compare_digest(sig, _sign(secret, note_id_s, expires_s)):
        log.info("share link rejected: bad signature")
        raise Forbidden("invalid link")
    if now >= int(expires_s):
        log.info("share link rejected: expired")
        raise Forbidden("invalid link")
    return int(note_id_s)
