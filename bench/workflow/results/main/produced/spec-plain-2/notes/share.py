import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden

logger = logging.getLogger("notes.share")

_LINK_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9a-f]{64})")


def _key(secret):
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


def _sign(secret, note_id_text, expires_text):
    msg = ("%s:%s" % (note_id_text, expires_text)).encode("ascii")
    return hmac.new(_key(secret), msg, hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    if now is None:
        now = time.time()
    expires = int(now) + int(ttl_sec)
    nid = str(int(note_id))
    exp = str(expires)
    return "%s.%s.%s" % (nid, exp, _sign(secret, nid, exp))


def verify_link(secret, link, now=None):
    if now is None:
        now = time.time()
    m = _LINK_RE.fullmatch(link) if isinstance(link, str) else None
    if not m:
        logger.info("share link rejected: malformed")
        raise Forbidden("invalid link")
    nid, exp, sig = m.groups()
    if not hmac.compare_digest(sig.encode("ascii"), _sign(secret, nid, exp).encode("ascii")):
        logger.info("share link rejected: bad signature")
        raise Forbidden("invalid link")
    if now >= int(exp):
        logger.info("share link rejected: expired")
        raise Forbidden("invalid link")
    return int(nid)
