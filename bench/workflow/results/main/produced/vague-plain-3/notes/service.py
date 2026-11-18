import logging
import time
from contextlib import contextmanager

from .errors import Conflict, Forbidden, NotFound, ValidationError

log = logging.getLogger("notes")

MAX_TITLE = 200
MAX_BODY = 100_000
MAX_TAG = 50
MAX_TAGS = 20
MAX_PER_PAGE = 100


class _Hooks:
    def before_write(self, step):
        pass


hooks = _Hooks()


# ---------- 共通 ----------

@contextmanager
def _txn(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _now():
    return int(time.time())


def _is_admin(user):
    return user.get("role") == "admin"


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _title(v):
    if not isinstance(v, str) or not v.strip() or len(v) > MAX_TITLE:
        raise ValidationError("title must be 1..%d chars" % MAX_TITLE)
    return v


def _body(v):
    if not isinstance(v, str) or len(v) > MAX_BODY:
        raise ValidationError("body must be a string up to %d chars" % MAX_BODY)
    return v


def _tags(v):
    if isinstance(v, (str, bytes)) or v is None:
        raise ValidationError("tags must be a list of strings")
    try:
        items = list(v)
    except TypeError:
        raise ValidationError("tags must be a list of strings")
    out = set()
    for t in items:
        if not isinstance(t, str):
            raise ValidationError("tag must be a string")
        t = t.strip()
        if not t or len(t) > MAX_TAG:
            raise ValidationError("tag must be 1..%d chars" % MAX_TAG)
        out.add(t)
    if len(out) > MAX_TAGS:
        raise ValidationError("too many tags")
    return sorted(out)


def _fetch(conn, user, note_id):
    """見えるメモの行を返す。他組織・他人の非公開は NotFound（存在も隠す）。"""
    if not _is_int(note_id):
        raise ValidationError("note_id must be an integer")
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None or row["org_id"] != user["org_id"] or (row["private"] and row["owner_id"] != user["id"]):
        raise NotFound("note not found")
    return row


def _check_can_modify(user, row):
    if row["owner_id"] != user["id"] and not _is_admin(user):
        raise Forbidden("only the owner or an admin may modify this note")


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _visible_sql():
    return "n.org_id = ? AND (n.private = 0 OR n.owner_id = ?)"


# ---------- メモ ----------

def create_note(conn, user, title, body, tags=(), private=False):
    title, body, tags = _title(title), _body(body), _tags(tags)
    if not isinstance(private, bool):
        raise ValidationError("private must be a bool")
    with _txn(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, int(private), _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in tags])
        _audit(conn, user, "create", f"note={note_id}")
    log.info("note created id=%s by user=%s", note_id, user["id"])
    return note_id


def get_note(conn, user, note_id):
    _fetch(conn, user, note_id)
    hooks.before_write("view")
    with _txn(conn):
        _fetch(conn, user, note_id)  # トランザクション内で再確認（消えた・非公開に変わった場合）
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = dict(conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())
        row["tags"] = [r["tag"] for r in conn.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
    return row


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if not _is_int(page) or page < 1:
        raise ValidationError("page must be >= 1")
    if not _is_int(per_page) or not 1 <= per_page <= MAX_PER_PAGE:
        raise ValidationError("per_page must be 1..%d" % MAX_PER_PAGE)
    order = {"updated": "n.updated_at DESC, n.id DESC", "title": "n.title ASC, n.id ASC"}.get(sort)
    if order is None:
        raise ValidationError("invalid sort")
    args = (user["org_id"], user["id"])
    total = conn.execute(f"SELECT COUNT(*) FROM notes n WHERE {_visible_sql()}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT n.id, n.title, n.updated_at FROM notes n WHERE {_visible_sql()} "
        f"ORDER BY {order} LIMIT ? OFFSET ?", args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str):
        raise ValidationError("q must be a string")
    tags = _tags(tags)
    sql = f"SELECT n.id, n.title FROM notes n WHERE {_visible_sql()}"
    args = [user["org_id"], user["id"]]
    if q:
        pat = "%" + _escape_like(q) + "%"
        sql += " AND (n.title LIKE ? ESCAPE '\\' OR n.body LIKE ? ESCAPE '\\')"
        args += [pat, pat]
    for t in tags:  # 指定したタグをすべて持つメモ
        sql += " AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id AND t.tag = ?)"
        args.append(t)
    sql += " ORDER BY n.id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    if not _is_int(version):
        raise ValidationError("version must be an integer")
    if title is not None:
        title = _title(title)
    if body is not None:
        body = _body(body)
    if tags is not None:
        tags = _tags(tags)
    _check_can_modify(user, _fetch(conn, user, note_id))
    hooks.before_write("update")
    with _txn(conn):
        row = _fetch(conn, user, note_id)  # 書き込み権を取った後に再確認
        _check_can_modify(user, row)
        if row["version"] != version:
            raise Conflict("note was modified by someone else")
        new_version = version + 1
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = ?, updated_at = ? WHERE id = ? AND version = ?",
            (row["title"] if title is None else title, row["body"] if body is None else body,
             new_version, _now(), note_id, version))
        if cur.rowcount != 1:
            raise Conflict("note was modified by someone else")
        if tags is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in tags])
        _audit(conn, user, "update", f"note={note_id}")
    return new_version


def delete_note(conn, user, note_id):
    _check_can_modify(user, _fetch(conn, user, note_id))
    with _txn(conn):
        _check_can_modify(user, _fetch(conn, user, note_id))
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")


# ---------- 管理者の操作 ----------

def _require_admin(user):
    if not _is_admin(user):
        raise Forbidden("admin only")


def _user_id(u):
    uid = u.get("id") if isinstance(u, dict) else u
    if not _is_int(uid):
        raise ValidationError("invalid user")
    return uid


def transfer_notes(conn, admin, from_user, to_user):
    _require_admin(admin)
    from_id, to_id = _user_id(from_user), _user_id(to_user)
    if from_id == to_id:
        raise ValidationError("from_user and to_user must differ")
    with _txn(conn):
        for uid in (from_id, to_id):  # 両方とも管理者と同じ組織の利用者であること
            r = conn.execute("SELECT org_id FROM users WHERE id = ?", (uid,)).fetchone()
            if r is None or r["org_id"] != admin["org_id"]:
                raise NotFound("user not found")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id", (from_id, admin["org_id"]))]
        for nid in ids:
            hooks.before_write(f"transfer:{nid}")
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1, updated_at = ? WHERE id = ?",
                         (to_id, _now(), nid))
        _audit(conn, admin, "transfer", f"from={from_id} to={to_id} count={len(ids)}")
    return len(ids)


def _mask(key):
    if not key:
        return ""
    return "****" + key[-4:] if len(key) > 8 else "****"


def export_audit(conn, admin):
    _require_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name AS actor, u.api_key AS api_key "
        "FROM audit a LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"], "actor": r["actor"],
             "actor_key": _mask(r["api_key"])} for r in rows]
