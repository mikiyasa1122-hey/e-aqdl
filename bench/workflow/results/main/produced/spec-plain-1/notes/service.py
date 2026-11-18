"""メモの作成・閲覧・一覧/検索・更新・削除・移管・監査ログ。"""
import logging
import re
import time
from contextlib import contextmanager

from .errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

_TAG_RE = re.compile(r"[a-z0-9-]{1,20}")
_SORTS = {
    "updated": "updated_at DESC, id DESC",
    "title": "title ASC, id ASC",
}


class _Hooks:
    def before_write(self, step):
        pass


hooks = _Hooks()


# ---- 共通 ----------------------------------------------------------------

@contextmanager
def _tx(conn):
    """書き込みトランザクション。失敗したら必ずロールバックして例外を伝える。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_title(title):
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValidationError("invalid title")


def _check_body(body):
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("invalid body")


def _check_tags(tags):
    """検査して、重複を除いたタグのリスト（出現順）を返す。"""
    if isinstance(tags, (str, bytes)) or tags is None:
        raise ValidationError("tags must be a sequence of strings")
    try:
        tags = list(tags)
    except TypeError:
        raise ValidationError("tags must be a sequence of strings") from None
    if len(tags) > 5:
        raise ValidationError("too many tags")
    for t in tags:
        if not isinstance(t, str) or not _TAG_RE.fullmatch(t):
            raise ValidationError("invalid tag")
    return list(dict.fromkeys(tags))


def _fetch(conn, note_id):
    if not _is_int(note_id):
        return None
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def _visible(user, row):
    if row is None or row["org_id"] != user["org_id"]:
        return False
    return not row["private"] or row["owner_id"] == user["id"]


def _load_visible(conn, user, note_id):
    row = _fetch(conn, note_id)
    if not _visible(user, row):
        raise NotFound("note not found")
    return row


def _load_writable(conn, user, note_id):
    row = _load_visible(conn, user, note_id)
    if row["owner_id"] != user["id"] and user["role"] != "admin":
        raise Forbidden("not allowed")
    return row


def _set_tags(conn, note_id, tags):
    conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                     [(note_id, t) for t in tags])


# ---- R1 作成 -------------------------------------------------------------

def create_note(conn, user, title, body, tags=(), private=False):
    _check_title(title)
    _check_body(body)
    tag_list = _check_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, int(time.time())))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        _set_tags(conn, note_id, tag_list)
        _audit(conn, user, "create", "note=%d" % note_id)
    logger.info("note created id=%d owner=%d", note_id, user["id"])
    return note_id


# ---- R2 閲覧 -------------------------------------------------------------

def get_note(conn, user, note_id):
    _load_visible(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        # ロック下で見直す（間に削除・移管されても漏らさない）
        _load_visible(conn, user, note_id)
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        tags = [r["tag"] for r in conn.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
    result = dict(row)
    result["tags"] = tags
    return result


# ---- R3 一覧・検索 -------------------------------------------------------

def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if not _is_int(page) or page < 1:
        raise ValidationError("invalid page")
    if not _is_int(per_page) or not 1 <= per_page <= 50:
        raise ValidationError("invalid per_page")
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ValidationError("invalid sort")
    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
    args = (user["org_id"], user["id"])
    total = conn.execute("SELECT COUNT(*) FROM notes WHERE " + where, args).fetchone()[0]
    rows = conn.execute(
        "SELECT id, title, updated_at FROM notes WHERE " + where +
        " ORDER BY " + _SORTS[sort] + " LIMIT ? OFFSET ?",   # sort は固定の表から選ぶ
        args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total,
            "page": page, "per_page": per_page}


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or not q.strip() or len(q) > 100:
        raise ValidationError("invalid query")
    tag_list = _check_tags(tags)
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    args = [user["org_id"], user["id"], pattern, pattern]
    if tag_list:
        sql += (" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = notes.id AND t.tag IN (%s))"
                % ",".join("?" * len(tag_list)))
        args.extend(tag_list)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---- R4 更新・削除 -------------------------------------------------------

def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    if title is not None:
        _check_title(title)
    if body is not None:
        _check_body(body)
    tag_list = _check_tags(tags) if tags is not None else None
    row = _load_writable(conn, user, note_id)
    if row["version"] != version:
        raise Conflict("version mismatch")
    hooks.before_write("update")
    with _tx(conn):
        row = _load_writable(conn, user, note_id)   # ロック下で見直す
        if row["version"] != version:
            raise Conflict("version mismatch")
        new_version = version + 1
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (row["title"] if title is None else title,
             row["body"] if body is None else body,
             new_version, int(time.time()), note_id, version))
        if cur.rowcount != 1:
            raise Conflict("version mismatch")
        if tag_list is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _set_tags(conn, note_id, tag_list)
        _audit(conn, user, "update", "note=%d" % note_id)
    return new_version


def delete_note(conn, user, note_id):
    _load_writable(conn, user, note_id)
    with _tx(conn):
        _load_writable(conn, user, note_id)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", "note=%d" % note_id)


# ---- R5 移管 -------------------------------------------------------------

def _user_ref(conn, ref):
    uid = ref["id"] if isinstance(ref, dict) else ref
    if not _is_int(uid):
        raise Forbidden("not allowed")
    row = conn.execute("SELECT id, org_id FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        raise Forbidden("not allowed")
    return row


def _require_admin(user):
    if user["role"] != "admin":
        raise Forbidden("admin only")


def transfer_notes(conn, admin, from_user, to_user):
    _require_admin(admin)
    src = _user_ref(conn, from_user)
    dst = _user_ref(conn, to_user)
    if not (src["org_id"] == dst["org_id"] == admin["org_id"]):
        raise Forbidden("not allowed")
    with _tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (src["id"], admin["org_id"]))]
        for nid in ids:
            hooks.before_write("transfer:%d" % nid)
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?",
                         (dst["id"], nid))
        _audit(conn, admin, "transfer",
               "from=%d to=%d count=%d" % (src["id"], dst["id"], len(ids)))
    return len(ids)


# ---- R6 監査ログ ---------------------------------------------------------

def _mask(key):
    key = key if isinstance(key, str) else ""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def export_audit(conn, admin):
    _require_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name AS actor, u.api_key AS api_key "
        "FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "WHERE a.org_id = ? ORDER BY a.id", (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["actor"], "actor_key": _mask(r["api_key"])} for r in rows]
