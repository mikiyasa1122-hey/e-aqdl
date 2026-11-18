import contextlib
import logging
import re
import time

from .errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

_TAG_RE = re.compile(r"[a-z0-9-]{1,20}")


class _Hooks:
    def before_write(self, step):
        pass


hooks = _Hooks()


# ---------- 検証 ----------
def _check_title(title):
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValidationError("invalid title")
    return title


def _check_body(body):
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("invalid body")
    return body


def _check_tags(tags):
    if isinstance(tags, (str, bytes)) or tags is None:
        raise ValidationError("tags must be a sequence of tags")
    try:
        tags = list(tags)
    except TypeError:
        raise ValidationError("tags must be a sequence of tags")
    if len(tags) > 5:
        raise ValidationError("too many tags")
    for t in tags:
        if not isinstance(t, str) or not _TAG_RE.fullmatch(t):
            raise ValidationError("invalid tag")
    return sorted(set(tags))


def _check_int(v, name):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValidationError("invalid %s" % name)
    return v


def _now():
    return int(time.time())


@contextlib.contextmanager
def _tx(conn):
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


def _visible_row(conn, user, note_id):
    """見られるメモの行。見られない・存在しない場合は NotFound。"""
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if (row is None or row["org_id"] != user["org_id"]
            or (row["private"] and row["owner_id"] != user["id"])):
        raise NotFound("note not found")
    return row


def _writable_row(conn, user, note_id):
    row = _visible_row(conn, user, note_id)
    if row["owner_id"] != user["id"] and user["role"] != "admin":
        raise Forbidden("not allowed")
    return row


def _tags_of(conn, note_id):
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]


def _insert_tags(conn, note_id, tags):
    conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                     [(note_id, t) for t in tags])


# ---------- R1 ----------
def create_note(conn, user, title, body, tags=(), private=False):
    _check_title(title)
    _check_body(body)
    tags = _check_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        _insert_tags(conn, note_id, tags)
        _audit(conn, user, "create", "note=%d" % note_id)
    logger.info("note created id=%d owner=%d", note_id, user["id"])
    return note_id


# ---------- R2 ----------
def get_note(conn, user, note_id):
    _visible_row(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        _visible_row(conn, user, note_id)  # 待っている間に消えた・見えなくなった場合
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


# ---------- R3 ----------
_VISIBLE_SQL = "n.org_id = ? AND (n.private = 0 OR n.owner_id = ?)"


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    _check_int(page, "page")
    _check_int(per_page, "per_page")
    if page < 1 or not 1 <= per_page <= 50 or sort not in ("updated", "title"):
        raise ValidationError("invalid paging")
    order = "n.updated_at DESC, n.id DESC" if sort == "updated" else "n.title ASC, n.id ASC"
    args = (user["org_id"], user["id"])
    total = conn.execute("SELECT COUNT(*) FROM notes n WHERE " + _VISIBLE_SQL, args).fetchone()[0]
    rows = conn.execute(
        "SELECT n.id, n.title, n.updated_at FROM notes n WHERE " + _VISIBLE_SQL +
        " ORDER BY " + order + " LIMIT ? OFFSET ?",
        args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or not q.strip() or len(q) > 100:
        raise ValidationError("invalid query")
    tags = _check_tags(tags)
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = ("SELECT n.id, n.title FROM notes n WHERE " + _VISIBLE_SQL +
           " AND (n.title LIKE ? ESCAPE '\\' OR n.body LIKE ? ESCAPE '\\')")
    args = [user["org_id"], user["id"], pattern, pattern]
    if tags:
        # 組み立てるのは ? の個数だけ。値は必ずパラメータで渡す
        sql += (" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id AND t.tag IN (" +
                ",".join("?" * len(tags)) + "))")
        args.extend(tags)
    sql += " ORDER BY n.id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------- R4 ----------
def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    _check_int(version, "version")
    if title is not None:
        _check_title(title)
    if body is not None:
        _check_body(body)
    if tags is not None:
        tags = _check_tags(tags)
    row = _writable_row(conn, user, note_id)
    if row["version"] != version:
        raise Conflict("version mismatch")
    hooks.before_write("update")
    with _tx(conn):
        row = _writable_row(conn, user, note_id)
        if row["version"] != version:
            raise Conflict("version mismatch")
        new_version = version + 1
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (row["title"] if title is None else title,
             row["body"] if body is None else body,
             new_version, _now(), note_id, version))
        if cur.rowcount != 1:
            raise Conflict("version mismatch")
        if tags is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, tags)
        _audit(conn, user, "update", "note=%d" % note_id)
    return new_version


def delete_note(conn, user, note_id):
    _writable_row(conn, user, note_id)
    with _tx(conn):
        _writable_row(conn, user, note_id)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", "note=%d" % note_id)


# ---------- R5 ----------
def transfer_notes(conn, admin, from_user, to_user):
    if admin["role"] != "admin":
        raise Forbidden("admin only")
    if not (admin["org_id"] == from_user["org_id"] == to_user["org_id"]):
        raise Forbidden("different organization")
    count = 0
    with _tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (from_user["id"], admin["org_id"]))]
        for nid in ids:
            hooks.before_write("transfer:%d" % nid)
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?",
                         (to_user["id"], nid))
            count += 1
        _audit(conn, admin, "transfer",
               "from=%d to=%d count=%d" % (from_user["id"], to_user["id"], count))
    return count


# ---------- R6 ----------
def _mask_key(key):
    key = key or ""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def export_audit(conn, admin):
    if admin["role"] != "admin":
        raise Forbidden("admin only")
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name AS actor, u.api_key AS api_key "
        "FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "WHERE a.org_id = ? ORDER BY a.id", (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["actor"], "actor_key": _mask_key(r["api_key"])} for r in rows]
