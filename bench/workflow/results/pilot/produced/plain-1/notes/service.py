import logging
import re
import time
from contextlib import contextmanager

from .errors import Conflict, Forbidden, NotFound, ValidationError

log = logging.getLogger("notes")

_TAG = re.compile(r"[a-z0-9-]{1,20}")


class _Hooks:
    def before_write(self, step):
        pass


hooks = _Hooks()


# ---------- 内部の道具 ----------

@contextmanager
def _tx(conn):
    """書き込みのトランザクション。例外ならロールバックし、接続を使える状態に戻す。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _now():
    return int(time.time())


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_title(title):
    if not isinstance(title, str) or not (1 <= len(title) <= 100) or not title.strip():
        raise ValidationError("invalid title")


def _check_body(body):
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("invalid body")


def _check_tags(tags):
    if tags is None or isinstance(tags, (str, bytes, dict)):
        raise ValidationError("invalid tags")
    try:
        tags = list(tags)
    except TypeError:
        raise ValidationError("invalid tags") from None
    if len(tags) > 5:
        raise ValidationError("too many tags")
    for t in tags:
        if not isinstance(t, str) or not _TAG.fullmatch(t):
            raise ValidationError("invalid tag")
    return sorted(set(tags))


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _visible(row, user):
    return (row is not None and row["org_id"] == user["org_id"]
            and (not row["private"] or row["owner_id"] == user["id"]))


def _load_visible(conn, user, note_id):
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not _visible(row, user):
        raise NotFound("note not found")
    return row


def _can_modify(row, user):
    return row["owner_id"] == user["id"] or user["role"] == "admin"


def _tags_of(conn, note_id):
    return [r[0] for r in conn.execute(
        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]


def _replace_tags(conn, note_id, tags):
    conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                     [(note_id, t) for t in tags])


# ---------- R1 ----------

def create_note(conn, user, title, body, tags=(), private=False):
    _check_title(title)
    _check_body(body)
    tags = _check_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        _replace_tags(conn, note_id, tags)
        _audit(conn, user, "create", f"note={note_id}")
    log.info("note created id=%s owner=%s", note_id, user["id"])
    return note_id


# ---------- R2 ----------

def get_note(conn, user, note_id):
    _load_visible(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        # 見られるかの確認と加算を、同じ書き込みロックの中で行う
        cur = conn.execute(
            "UPDATE notes SET views = views + 1"
            " WHERE id = ? AND org_id = ? AND (private = 0 OR owner_id = ?)",
            (note_id, user["org_id"], user["id"]))
        if cur.rowcount == 0:
            raise NotFound("note not found")
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


# ---------- R3 ----------

def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if not _is_int(page) or page < 1:
        raise ValidationError("invalid page")
    if not _is_int(per_page) or not (1 <= per_page <= 50):
        raise ValidationError("invalid per_page")
    if sort == "updated":
        order = "updated_at DESC, id DESC"
    elif sort == "title":
        order = "title ASC, id ASC"
    else:
        raise ValidationError("invalid sort")
    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
    args = (user["org_id"], user["id"])
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, title, updated_at FROM notes WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or not (1 <= len(q) <= 100) or not q.strip():
        raise ValidationError("invalid query")
    tags = _check_tags(tags)
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?)"
           " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    args = [user["org_id"], user["id"], like, like]
    if tags:
        sql += (" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = notes.id AND t.tag IN ("
                + ",".join("?" * len(tags)) + "))")
        args += tags
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------- R4 ----------

def _check_modifiable(row, user):
    if not _can_modify(row, user):
        raise Forbidden("not allowed")


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    if title is not None:
        _check_title(title)
    if body is not None:
        _check_body(body)
    if tags is not None:
        tags = _check_tags(tags)
    row = _load_visible(conn, user, note_id)
    _check_modifiable(row, user)
    if row["version"] != version:
        raise Conflict("version conflict")
    hooks.before_write("update")
    with _tx(conn):
        # ロックを得た後で、状態を読み直して確かめる
        row = _load_visible(conn, user, note_id)
        _check_modifiable(row, user)
        if row["version"] != version:
            raise Conflict("version conflict")
        new_version = version + 1
        conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = ?, updated_at = ? WHERE id = ?",
            (row["title"] if title is None else title,
             row["body"] if body is None else body,
             new_version, _now(), note_id))
        if tags is not None:
            _replace_tags(conn, note_id, tags)
        _audit(conn, user, "update", f"note={note_id}")
    return new_version


def delete_note(conn, user, note_id):
    row = _load_visible(conn, user, note_id)
    _check_modifiable(row, user)
    with _tx(conn):
        row = _load_visible(conn, user, note_id)
        _check_modifiable(row, user)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")


# ---------- R5 ----------

def _user_id(u):
    return u["id"] if isinstance(u, dict) else u


def transfer_notes(conn, admin, from_user, to_user):
    if admin.get("role") != "admin":
        raise Forbidden("not allowed")
    src, dst = _user_id(from_user), _user_id(to_user)
    with _tx(conn):
        orgs = {}
        for uid in (src, dst):
            r = conn.execute("SELECT org_id FROM users WHERE id = ?", (uid,)).fetchone()
            orgs[uid] = r["org_id"] if r else None
        if orgs[src] != admin["org_id"] or orgs[dst] != admin["org_id"]:
            raise Forbidden("not allowed")
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (src, admin["org_id"]))]
        for nid in ids:
            hooks.before_write(f"transfer:{nid}")
            conn.execute(
                "UPDATE notes SET owner_id = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (dst, _now(), nid))
        _audit(conn, admin, "transfer", f"from={src} to={dst} count={len(ids)}")
    return len(ids)


# ---------- R6 ----------

def _mask(key):
    key = key or ""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def export_audit(conn, admin):
    if admin.get("role") != "admin":
        raise Forbidden("not allowed")
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a"
        " LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["name"], "actor_key": _mask(r["api_key"])} for r in rows]
