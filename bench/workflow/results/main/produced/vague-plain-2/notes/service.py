"""メモの業務ロジック。

方針:
- 他の組織のメモ・他人の非公開メモは「存在しない」(NotFound) として扱い、存在を漏らさない。
- 見えるが権限のない操作は Forbidden。非公開メモは管理者でも持ち主以外は触れない
  (引き継ぎ transfer_notes だけは管理者が行える)。
- 書き込みは BEGIN IMMEDIATE のトランザクションで行い、トランザクション内で
  存在・権限・version を確かめ直す (検査と書き込みの間の変更に備える)。
- 更新は version による楽観的ロック。食い違えば Conflict。
"""
import logging
import time
from contextlib import contextmanager

from .errors import Conflict, Forbidden, NotFound, ValidationError

log = logging.getLogger("notes")

MAX_TITLE = 200
MAX_BODY = 100_000
MAX_TAGS = 20
MAX_TAG_LEN = 50
MAX_PER_PAGE = 100


class _Hooks:
    def before_write(self, step):
        pass


hooks = _Hooks()
# RateLimiter を設定すると、書き込み系の操作の先頭で hit(user["id"]) を呼ぶ。
rate_limiter = None


# ---------- 補助 ----------

def _now():
    return int(time.time())


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


@contextmanager
def _tx(conn):
    if conn.in_transaction:  # 呼び出し側のトランザクションに参加する
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _throttle(user):
    if rate_limiter is not None:
        rate_limiter.hit(user["id"])


def _check_user(user):
    if not isinstance(user, dict) or not _is_int(user.get("id")) or not _is_int(user.get("org_id")):
        raise ValidationError("invalid user")
    if user.get("role") not in ("member", "admin"):
        raise ValidationError("invalid role")


def _is_admin(user):
    return user["role"] == "admin"


def _clean_title(title):
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("title must be a non-empty string")
    if len(title) > MAX_TITLE:
        raise ValidationError("title too long")
    return title


def _clean_body(body):
    if not isinstance(body, str):
        raise ValidationError("body must be a string")
    if len(body) > MAX_BODY:
        raise ValidationError("body too long")
    return body


def _clean_tags(tags):
    if isinstance(tags, (str, bytes)) or tags is None:
        raise ValidationError("tags must be a sequence of strings")
    try:
        items = list(tags)
    except TypeError:
        raise ValidationError("tags must be a sequence of strings")
    out = []
    for t in items:
        if not isinstance(t, str):
            raise ValidationError("tag must be a string")
        t = t.strip()
        if not t or len(t) > MAX_TAG_LEN:
            raise ValidationError("invalid tag")
        if t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ValidationError("too many tags")
    return out


def _fetch(conn, user, note_id):
    """見えるメモの行を返す。見えなければ NotFound。"""
    if not _is_int(note_id):
        raise ValidationError("invalid note id")
    row = conn.execute("SELECT * FROM notes WHERE id = ? AND org_id = ?",
                       (note_id, user["org_id"])).fetchone()
    if row is None or (row["private"] and row["owner_id"] != user["id"]):
        raise NotFound("note not found")
    return row


def _fetch_writable(conn, user, note_id):
    row = _fetch(conn, user, note_id)
    if row["owner_id"] != user["id"] and not _is_admin(user):
        raise Forbidden("not allowed")
    return row


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _tags_of(conn, note_id):
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]


def _visible_sql():
    return "org_id = ? AND (private = 0 OR owner_id = ?)"


# ---------- 公開 API ----------

def create_note(conn, user, title, body, tags=(), private=False):
    _check_user(user)
    title = _clean_title(title)
    body = _clean_body(body)
    tags = _clean_tags(tags)
    if not isinstance(private, bool):
        raise ValidationError("private must be bool")
    _throttle(user)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                         [(note_id, t) for t in tags])
        _audit(conn, user, "create", "note=%d" % note_id)
    log.info("note created id=%d owner=%d", note_id, user["id"])
    return note_id


def get_note(conn, user, note_id):
    _check_user(user)
    _fetch(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        _fetch(conn, user, note_id)  # 間に消された・非公開になった場合に備える
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    _check_user(user)
    if not _is_int(page) or page < 1:
        raise ValidationError("invalid page")
    if not _is_int(per_page) or not 1 <= per_page <= MAX_PER_PAGE:
        raise ValidationError("invalid per_page")
    if sort == "updated":
        order = "updated_at DESC, id DESC"
    elif sort == "title":
        order = "title ASC, id ASC"
    else:
        raise ValidationError("invalid sort")
    args = (user["org_id"], user["id"])
    total = conn.execute("SELECT COUNT(*) FROM notes WHERE " + _visible_sql(), args).fetchone()[0]
    rows = conn.execute(
        "SELECT id, title, updated_at FROM notes WHERE " + _visible_sql() +
        " ORDER BY " + order + " LIMIT ? OFFSET ?",
        args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(conn, user, q, tags=()):
    """題名か本文の部分一致 (q が空なら絞らない)。tags は指定したものをすべて持つメモ。"""
    _check_user(user)
    if not isinstance(q, str):
        raise ValidationError("q must be a string")
    tags = _clean_tags(tags)
    sql = "SELECT id, title FROM notes WHERE " + _visible_sql()
    args = [user["org_id"], user["id"]]
    if q:
        pat = "%" + _like_escape(q) + "%"
        sql += " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
        args += [pat, pat]
    for t in tags:
        sql += " AND EXISTS (SELECT 1 FROM note_tags nt WHERE nt.note_id = notes.id AND nt.tag = ?)"
        args.append(t)
    sql += " ORDER BY id"
    return [{"id": r["id"], "title": r["title"]} for r in conn.execute(sql, args)]


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    _check_user(user)
    if not _is_int(version):
        raise ValidationError("invalid version")
    if title is None and body is None and tags is None:
        raise ValidationError("nothing to update")
    if title is not None:
        title = _clean_title(title)
    if body is not None:
        body = _clean_body(body)
    if tags is not None:
        tags = _clean_tags(tags)
    _throttle(user)
    _fetch_writable(conn, user, note_id)
    hooks.before_write("update")
    with _tx(conn):
        row = _fetch_writable(conn, user, note_id)
        new_version = version + 1
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (row["title"] if title is None else title,
             row["body"] if body is None else body,
             _now(), note_id, version))
        if cur.rowcount != 1:
            raise Conflict("version mismatch")
        if tags is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                             [(note_id, t) for t in tags])
        _audit(conn, user, "update", "note=%d" % note_id)
    return new_version


def delete_note(conn, user, note_id):
    _check_user(user)
    _throttle(user)
    with _tx(conn):
        _fetch_writable(conn, user, note_id)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", "note=%d" % note_id)


def _user_id(u):
    if isinstance(u, dict):
        u = u.get("id")
    if not _is_int(u):
        raise ValidationError("invalid user id")
    return u


def transfer_notes(conn, admin, from_user, to_user):
    """from_user の全メモ (非公開含む) を to_user に移す。引数は id でも利用者の dict でもよい。"""
    _check_user(admin)
    if not _is_admin(admin):
        raise Forbidden("admin only")
    src, dst = _user_id(from_user), _user_id(to_user)
    if src == dst:
        raise ValidationError("from and to are the same")
    _throttle(admin)
    with _tx(conn):
        for uid in (src, dst):  # 同じ組織の利用者だけ
            if conn.execute("SELECT 1 FROM users WHERE id = ? AND org_id = ?",
                            (uid, admin["org_id"])).fetchone() is None:
                raise NotFound("user not found")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (src, admin["org_id"]))]
        now = _now()
        for nid in ids:
            hooks.before_write("transfer:%d" % nid)
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1, updated_at = ? "
                         "WHERE id = ?", (dst, now, nid))
        _audit(conn, admin, "transfer", "from=%d to=%d count=%d" % (src, dst, len(ids)))
    return len(ids)


def _mask(key):
    if not isinstance(key, str) or len(key) < 8:
        return "****"
    return "****" + key[-4:]


def export_audit(conn, admin):
    _check_user(admin)
    if not _is_admin(admin):
        raise Forbidden("admin only")
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name AS actor, u.api_key AS api_key "
        "FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "WHERE a.org_id = ? ORDER BY a.id", (admin["org_id"],))
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["actor"], "actor_key": _mask(r["api_key"])} for r in rows]
