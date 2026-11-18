import contextlib
import logging
import re
import time

from .errors import Conflict, Forbidden, NotFound, ValidationError

log = logging.getLogger("notes")

TAG_RE = re.compile(r"[a-z0-9-]{1,20}")
SORTS = {
    "updated": "updated_at DESC, id DESC",
    "title": "title ASC, id ASC",
}


class _Hooks:
    def before_write(self, step):
        pass


hooks = _Hooks()


# ---- 補助 ----

@contextlib.contextmanager
def _txn(conn):
    """書き込みのトランザクション。失敗したらロールバックして例外をそのまま伝える。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_title(title):
    if not isinstance(title, str) or not (1 <= len(title) <= 100) or not title.strip():
        raise ValidationError("invalid title")


def _check_body(body):
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("invalid body")


def _check_tags(tags):
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple, set, frozenset)):
        raise ValidationError("tags must be a sequence of strings")
    tags = list(tags)
    if len(tags) > 5:
        raise ValidationError("too many tags")
    for t in tags:
        if not isinstance(t, str) or not TAG_RE.fullmatch(t):
            raise ValidationError("invalid tag")
    return sorted(set(tags))


def _now():
    return int(time.time())


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _visible(row, user):
    return (row is not None and row["org_id"] == user["org_id"]
            and (not row["private"] or row["owner_id"] == user["id"]))


def _fetch_visible(conn, user, note_id):
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not _visible(row, user):
        raise NotFound("note not found")
    return row


def _visible_sql():
    return "org_id = ? AND (private = 0 OR owner_id = ?)"


def _require_admin(user):
    if user.get("role") != "admin":
        raise Forbidden("admin only")


def _mask(key):
    key = key or ""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


# ---- R1 ----

def create_note(conn, user, title, body, tags=(), private=False):
    _check_title(title)
    _check_body(body)
    tag_list = _check_tags(tags)
    with _txn(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                         [(note_id, t) for t in tag_list])
        _audit(conn, user, "create", "note=%d" % note_id)
    log.info("note created id=%d owner=%d", note_id, user["id"])
    return note_id


# ---- R2 ----

def get_note(conn, user, note_id):
    _fetch_visible(conn, user, note_id)
    hooks.before_write("view")
    with _txn(conn):
        # 見られるかをトランザクション内でも確かめ、加算は DB 側で行う（数え漏れなし）
        _fetch_visible(conn, user, note_id)
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        tags = [r["tag"] for r in conn.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
    result = dict(row)
    result["tags"] = tags
    return result


# ---- R3 ----

def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if not _is_int(page) or page < 1:
        raise ValidationError("invalid page")
    if not _is_int(per_page) or not (1 <= per_page <= 50):
        raise ValidationError("invalid per_page")
    if not isinstance(sort, str) or sort not in SORTS:
        raise ValidationError("invalid sort")
    args = (user["org_id"], user["id"])
    total = conn.execute("SELECT COUNT(*) FROM notes WHERE " + _visible_sql(), args).fetchone()[0]
    rows = conn.execute(
        "SELECT id, title, updated_at FROM notes WHERE " + _visible_sql() +
        " ORDER BY " + SORTS[sort] + " LIMIT ? OFFSET ?",
        args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or not (1 <= len(q) <= 100) or not q.strip():
        raise ValidationError("invalid query")
    tag_list = _check_tags(tags)
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = ("SELECT id, title FROM notes WHERE " + _visible_sql() +
           " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    args = [user["org_id"], user["id"], pattern, pattern]
    if tag_list:
        sql += " AND id IN (SELECT note_id FROM note_tags WHERE tag IN (%s))" % ",".join("?" * len(tag_list))
        args.extend(tag_list)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args)]


# ---- R4 ----

def _check_can_modify(row, user):
    if row["owner_id"] != user["id"] and user.get("role") != "admin":
        raise Forbidden("not allowed")


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    if title is not None:
        _check_title(title)
    if body is not None:
        _check_body(body)
    tag_list = _check_tags(tags) if tags is not None else None
    row = _fetch_visible(conn, user, note_id)
    _check_can_modify(row, user)
    if row["version"] != version:
        raise Conflict("version mismatch")
    hooks.before_write("update")
    with _txn(conn):
        # 書き込みのロックを取った後で、もう一度確かめる（間に入った更新・削除を見逃さない）
        row = _fetch_visible(conn, user, note_id)
        _check_can_modify(row, user)
        new_version = row["version"] + 1
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (row["title"] if title is None else title,
             row["body"] if body is None else body,
             new_version, _now(), note_id, version))
        if cur.rowcount != 1:
            raise Conflict("version mismatch")
        if tag_list is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                             [(note_id, t) for t in tag_list])
        _audit(conn, user, "update", "note=%d" % note_id)
    return new_version


def delete_note(conn, user, note_id):
    with _txn(conn):
        row = _fetch_visible(conn, user, note_id)
        _check_can_modify(row, user)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", "note=%d" % note_id)


# ---- R5 ----

def transfer_notes(conn, admin, from_user, to_user):
    _require_admin(admin)
    if not (from_user["org_id"] == admin["org_id"] == to_user["org_id"]):
        raise Forbidden("different organization")
    with _txn(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (from_user["id"], admin["org_id"]))]
        for nid in ids:
            hooks.before_write("transfer:%d" % nid)
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?",
                         (to_user["id"], nid))
        _audit(conn, admin, "transfer",
               "from=%d to=%d count=%d" % (from_user["id"], to_user["id"], len(ids)))
    return len(ids)


# ---- R6 ----

def export_audit(conn, admin):
    _require_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name AS actor, u.api_key AS api_key "
        "FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "WHERE a.org_id = ? ORDER BY a.id", (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["actor"], "actor_key": _mask(r["api_key"])} for r in rows]
