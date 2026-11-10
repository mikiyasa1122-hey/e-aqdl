import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden

log = logging.getLogger("notes.share")
_LINK = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9a-f]{64})")


def _key(secret):
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


def _sign(secret, note_id, expires):
    msg = f"{note_id}:{expires}".encode("ascii")
    return hmac.new(_key(secret), msg, hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if now is None:
        now = time.time()
    expires = int(now + ttl_sec)
    return f"{note_id}.{expires}.{_sign(secret, note_id, expires)}"


def verify_link(secret, link, now=None):
    if now is None:
        now = time.time()
    m = _LINK.fullmatch(link) if isinstance(link, str) else None
    if not m:
        log.info("share link rejected: malformed")
        raise Forbidden("invalid link")
    note_id, expires, sig = m.groups()
    if not hmac.compare_digest(sig, _sign(secret, note_id, expires)):
        log.info("share link rejected: bad signature")
        raise Forbidden("invalid link")
    if now >= int(expires):
        log.info("share link rejected: expired")
        raise Forbidden("invalid link")
    return int(note_id)
