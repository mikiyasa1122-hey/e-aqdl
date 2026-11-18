"""メモの共有リンク（署名付き・期限付き）。"""
import hashlib
import hmac
import logging
import re
import time

from .errors import Forbidden

log = logging.getLogger("notes.share")
LINK_RE = re.compile(r"([0-9]{1,18})\.([0-9]{1,18})\.([0-9a-f]{64})")  # fullmatch で使う（改行・他の数字を通さない）


def _sign(secret, note_id, expires):
    return hmac.new(secret.encode(), f"{note_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def make_link(secret, note_id, ttl_sec=3600, now=None):
    expires = int(now if now is not None else time.time()) + ttl_sec
    token = _sign(secret, note_id, expires)
    log.info("share link created note=%s expires=%s", note_id, expires)
    return f"{note_id}.{expires}.{token}"


def verify_link(secret, link, now=None):
    """有効なら note_id を返す。期限切れ（期限の時刻ちょうどを含む）・改ざんは Forbidden。"""
    if not isinstance(link, str):
        raise Forbidden("不正なリンク")
    try:
        m = LINK_RE.fullmatch(link)
        if not m:
            raise ValueError(link)
        note_id, expires, token = int(m.group(1)), int(m.group(2)), m.group(3)
    except ValueError:
        raise Forbidden("不正なリンク") from None
    if int(now if now is not None else time.time()) >= expires:
        raise Forbidden("期限切れのリンク")
    if not hmac.compare_digest(token.encode("utf-8"), _sign(secret, note_id, expires).encode("utf-8")):
        raise Forbidden("不正なリンク")
    return note_id
