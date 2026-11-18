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


# ---- 検証 ----
def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_user(user):
    if not isinstance(user, dict) or not _is_int(user.get("id")) or not _is_int(user.get("org_id")) \
            or user.get("role") not in ("member", "admin"):
        raise ValidationError("invalid user")


def _check_admin(admin):
    _check_user(admin)
    if admin["role"] != "admin":
        raise Forbidden("admin only")


def _title(v):
    if not isinstance(v, str) or not v.strip():
        raise ValidationError("title must be a non-empty string")
    if len(v) > MAX_TITLE:
        raise ValidationError("title too long")
    return v


def _body(v):
    if not isinstance(v, str):
        raise ValidationError("body must be a string")
    if len(v) > MAX_BODY:
        raise ValidationError("body too long")
    return v


def _tags(tags):
    if isinstance(tags, (str, bytes)) or tags is None:
        raise ValidationError("tags must be a list of strings")
    out = set()
    try:
        items = list(tags)
    except TypeError:
        raise ValidationError("tags must be a list of strings")
    for t in items:
        if not isinstance(t, str) or not t.strip() or len(t.strip()) > MAX_TAG_LEN:
            raise ValidationError("invalid tag")
        out.add(t.strip())
    if len(out) > MAX_TAGS:
        raise ValidationError("too many tags")
    return sorted(out)


def _id(v, what="id"):
    if not _is_int(v):
        raise ValidationError("invalid " + what)
    return v


# ---- トランザクション ----
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


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _visible(user, row):
    """自分の組織のメモで、非公開なら本人のものだけ見える。"""
    return row is not None and row["org_id"] == user["org_id"] and \
        (not row["private"] or row["owner_id"] == user["id"])


def _can_modify(user, row):
    return row["owner_id"] == user["id"] or user["role"] == "admin"


def _fetch(conn, user, note_id):
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not _visible(user, row):
        raise NotFound("note not found")  # 他組織・他人の非公開は存在も明かさない
    return row


def _set_tags(conn, note_id, tags):
    conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                     [(note_id, t) for t in tags])


def _now():
    return int(time.time())


# ---- API ----
def create_note(conn, user, title, body, tags=(), private=False):
    _check_user(user)
    title, body, tags = _title(title), _body(body), _tags(tags)
    if not isinstance(private, bool):
        raise ValidationError("private must be bool")
    with _txn(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["org_id"], user["id"], title, body, int(private), _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        _set_tags(conn, note_id, tags)
        _audit(conn, user, "create", "note=%d" % note_id)
    log.info("note created id=%d by=%d", note_id, user["id"])
    return note_id


def get_note(conn, user, note_id):
    _check_user(user)
    _id(note_id, "note_id")
    _fetch(conn, user, note_id)
    hooks.before_write("view")
    with _txn(conn):
        _fetch(conn, user, note_id)  # 確認後に消された場合
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        tags = [r["tag"] for r in conn.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
    d = dict(row)
    d["tags"] = tags
    return d


_VISIBLE_SQL = "org_id = ? AND (private = 0 OR owner_id = ?)"


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    _check_user(user)
    if not _is_int(page) or page < 1 or not _is_int(per_page) or not 1 <= per_page <= MAX_PER_PAGE:
        raise ValidationError("invalid paging")
    orders = {"updated": "updated_at DESC, id DESC", "title": "title ASC, id ASC"}
    if sort not in orders:
        raise ValidationError("invalid sort")
    args = (user["org_id"], user["id"])
    total = conn.execute("SELECT COUNT(*) FROM notes WHERE " + _VISIBLE_SQL, args).fetchone()[0]
    rows = conn.execute(
        "SELECT id, title, updated_at FROM notes WHERE " + _VISIBLE_SQL +
        " ORDER BY " + orders[sort] + " LIMIT ? OFFSET ?",
        args + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def search_notes(conn, user, q, tags=()):
    _check_user(user)
    if not isinstance(q, str):
        raise ValidationError("q must be a string")
    tags = _tags(tags)
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pat = "%" + esc + "%"
    sql = ("SELECT id, title FROM notes WHERE " + _VISIBLE_SQL +
           " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    args = [user["org_id"], user["id"], pat, pat]
    for t in tags:  # 指定したタグをすべて持つメモ
        sql += " AND EXISTS (SELECT 1 FROM note_tags WHERE note_id = notes.id AND tag = ?)"
        args.append(t)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args)]


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    _check_user(user)
    _id(note_id, "note_id")
    if not _is_int(version):
        raise ValidationError("invalid version")
    if title is not None:
        title = _title(title)
    if body is not None:
        body = _body(body)
    if tags is not None:
        tags = _tags(tags)
    row = _fetch(conn, user, note_id)
    if not _can_modify(user, row):
        raise Forbidden("not allowed")
    hooks.before_write("update")
    with _txn(conn):
        row = _fetch(conn, user, note_id)  # 他の更新・削除・引き継ぎの後の状態で確かめ直す
        if not _can_modify(user, row):
            raise Forbidden("not allowed")
        # version が一致する時だけ書く（人の変更を上書きしない）
        cur = conn.execute(
            "UPDATE notes SET title = COALESCE(?, title), body = COALESCE(?, body), "
            "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (title, body, _now(), note_id, version))
        if cur.rowcount != 1:
            raise Conflict("note was modified by someone else")
        if tags is not None:
            _set_tags(conn, note_id, tags)
        _audit(conn, user, "update", "note=%d" % note_id)
    return version + 1


def delete_note(conn, user, note_id):
    _check_user(user)
    _id(note_id, "note_id")
    row = _fetch(conn, user, note_id)
    if not _can_modify(user, row):
        raise Forbidden("not allowed")
    with _txn(conn):
        row = _fetch(conn, user, note_id)
        if not _can_modify(user, row):
            raise Forbidden("not allowed")
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", "note=%d" % note_id)


def transfer_notes(conn, admin, from_user, to_user):
    _check_admin(admin)
    ids = []
    for u in (from_user, to_user):
        ids.append(u["id"] if isinstance(u, dict) and "id" in u else u)
    from_id, to_id = _id(ids[0], "from_user"), _id(ids[1], "to_user")
    if from_id == to_id:
        raise ValidationError("from_user and to_user must differ")
    with _txn(conn):
        # 同じ組織の利用者だけが対象（別の組織へメモが移らないように）
        for uid in (from_id, to_id):
            r = conn.execute("SELECT org_id FROM users WHERE id = ?", (uid,)).fetchone()
            if r is None or r["org_id"] != admin["org_id"]:
                raise NotFound("user not found")
        note_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE org_id = ? AND owner_id = ? ORDER BY id",
            (admin["org_id"], from_id))]
        for nid in note_ids:
            hooks.before_write("transfer:%d" % nid)
            conn.execute(
                "UPDATE notes SET owner_id = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (to_id, _now(), nid))
        _audit(conn, admin, "transfer", "from=%d to=%d count=%d" % (from_id, to_id, len(note_ids)))
    return len(note_ids)


def _mask(key):
    if not isinstance(key, str) or len(key) <= 8:
        return "****"
    return "****" + key[-4:]


def export_audit(conn, admin):
    _check_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name AS actor, u.api_key AS api_key "
        "FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "WHERE a.org_id = ? ORDER BY a.id", (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["actor"], "actor_key": _mask(r["api_key"])} for r in rows]
